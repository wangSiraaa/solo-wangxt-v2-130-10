from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import (
    Job,
    JobStage,
    JobStageAttempt,
    JobStatus,
    Snapshot,
    StageStatus,
)
from app.services import network
from app.services.solver import execute_solve
from app.workers.celery_app import celery_app

STAGES = ("import_qc", "component_precheck", "solve", "publish_checks")
LOCK_TTL_SECONDS = 60 * 60


def _lock_names(job_id: int) -> tuple[str, str]:
    return f"job-generation-dispatch:{job_id}", f"job-generation-lock:{job_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_job(db: Session, job_id: int) -> tuple[Job, dict[str, JobStage]]:
    job = db.get(Job, job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")
    stages = {s.name: s for s in job.stages}
    return job, stages


def _begin_stage(stage: JobStage) -> None:
    """Mark the next retry and leave its prior terminal attempts untouched."""
    stage.attempt += 1
    stage.status = StageStatus.RUNNING
    stage.started_at = _now()


def _finish_stage(
    db: Session,
    stage: JobStage,
    status: str,
    detail: dict[str, Any] | None = None,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if status not in (StageStatus.CONFIRMED, StageStatus.FAILED):
        raise ValueError(f"terminal stage status expected, got {status}")

    now = _now()
    stage.status = status
    stage.completed_at = now
    if status == StageStatus.CONFIRMED:
        stage.confirmed_at = now
    if detail is not None:
        stage.detail = detail

    attempt_number = stage.attempt or 1
    db.flush()
    db.add(
        JobStageAttempt(
            job_id=stage.job_id,
            stage_id=stage.id,
            attempt_number=attempt_number,
            status=status,
            detail=detail if detail is not None else stage.detail,
            error_code=error_code,
            error_message=error_message,
            started_at=stage.started_at,
            finished_at=now,
        )
    )


def _mark_interrupted(db: Session, stage: JobStage) -> None:
    if stage.status != StageStatus.RUNNING:
        return
    now = _now()
    detail = {"error": "worker process was interrupted before this stage reached a terminal marker"}
    stage.status = StageStatus.PENDING
    stage.completed_at = now
    db.add(
        JobStageAttempt(
            job_id=stage.job_id,
            stage_id=stage.id,
            attempt_number=stage.attempt,
            status="interrupted",
            detail=detail,
            error_code="worker_interrupted",
            error_message=detail["error"],
            started_at=stage.started_at,
            finished_at=now,
        )
    )


def _fail_current_running_stage(
    db: Session,
    job: Job,
    stages: dict[str, JobStage],
    error_code: str,
    exc: Exception,
) -> None:
    stage = stages.get(job.current_stage)
    if stage is None or stage.status != StageStatus.RUNNING or stage.attempt == 0:
        return
    detail = {"error": str(exc), "error_type": exc.__class__.__name__}
    _finish_stage(db, stage, StageStatus.FAILED, detail, error_code=error_code, error_message=str(exc))


@celery_app.task(name="pipeline.import_qc")
def import_qc(job_id: int) -> int:
    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        if stages["import_qc"].status == StageStatus.CONFIRMED:
            return job_id
        job.status = JobStatus.RUNNING
        job.current_stage = "import_qc"
        job.attempt += 1
        job.error_code = None
        job.error_message = None
        _begin_stage(stages["import_qc"])
        db.commit()

        rows = job.snapshot.payload["observations"]
        point_ids = [point["id"] for point in job.snapshot.payload["points"]]
        size = (len(rows) + 7) // 8
        chunks = [rows[index * size : (index + 1) * size] for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            qc = list(executor.map(lambda args: qc_partition.run(*args), ((chunk, point_ids, index) for index, chunk in enumerate(chunks))))
        failed = [p for p in qc if not p["ok"]]
        detail = {"partitions": qc, "failed_count": len(failed)}
        if failed:
            _finish_stage(
                db,
                stages["import_qc"],
                StageStatus.FAILED,
                detail,
                error_code="import_qc_failed",
                error_message=f"{len(failed)} QC partitions failed",
            )
            job.status = JobStatus.FAILED
            job.error_code = "import_qc_failed"
            job.error_message = f"{len(failed)} QC partitions failed"
        else:
            _finish_stage(db, stages["import_qc"], StageStatus.CONFIRMED, detail)
        db.commit()
        return job_id
    finally:
        db.close()


@celery_app.task(name="pipeline.component_precheck")
def component_precheck(job_id: int) -> int:
    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        if stages["component_precheck"].status == StageStatus.CONFIRMED:
            return job_id
        if stages["import_qc"].status != StageStatus.CONFIRMED:
            raise RuntimeError("previous stage import_qc was not confirmed")
        job.status = JobStatus.RUNNING
        job.current_stage = "component_precheck"
        job.error_code = None
        job.error_message = None
        _begin_stage(stages["component_precheck"])
        db.commit()

        payload = job.snapshot.payload
        components = network.build_components(payload["points"], payload["observations"])
        datum_points = {d["point_id"] for d in payload["datums"]}
        component_obs_count = [0 for _ in components]
        point_to_component: dict[int, int] = {}
        for index, local_indices in enumerate(components):
            for local_index in local_indices:
                point_to_component[payload["points"][local_index]["id"]] = index
        for observation in payload["observations"]:
            component_index = point_to_component.get(observation["from_point_id"])
            if component_index is not None and point_to_component.get(observation["to_point_id"]) == component_index:
                component_obs_count[component_index] += 1
        component_report = []
        for index, local_indices in enumerate(components):
            point_ids = {payload["points"][i]["id"] for i in local_indices}
            component_report.append(
                {
                    "component": index,
                    "point_count": len(point_ids),
                    "observation_count": component_obs_count[index],
                    "datum_count": len(point_ids & datum_points),
                    "sample_points": sorted(point_ids)[:20],
                }
            )
        bad = [c for c in component_report if c["datum_count"] == 0]
        detail = {
            "components": component_report,
            "parallel_precheck_allowed": True,
            "stitching_components": False,
            "solve_model": "one global sparse model over all connected component blocks",
        }
        # A no-datum component is reported the same day, but the solve stage still
        # runs its QR diagnosis and refuses a fabricated vertical datum.
        _finish_stage(db, stages["component_precheck"], StageStatus.CONFIRMED, detail)
        if bad:
            job.diagnostics = {"component_warnings": bad}
        db.commit()
        return job_id
    finally:
        db.close()


@celery_app.task(name="pipeline.solve")
def solve(job_id: int) -> int:
    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        if stages["solve"].status == StageStatus.CONFIRMED:
            return job_id
        if stages["component_precheck"].status != StageStatus.CONFIRMED:
            raise RuntimeError("component_precheck was not confirmed")
        job.status = JobStatus.RUNNING
        job.current_stage = "solve"
        job.error_code = None
        job.error_message = None
        _begin_stage(stages["solve"])
        db.commit()
        try:
            output = execute_solve(db, job)
            diagnostics = output["diagnostics"]
            blocked = diagnostics["blocked_components"]
            closure_failed = diagnostics["pre_adjustment_closures"]["failed"]
            detail = {
                "component_count": diagnostics["component_count"],
                "blocked_count": len(blocked),
                "blocked_components": blocked,
                "pre_closure_failed_count": len(closure_failed),
                "residual_statistics": diagnostics["residual_statistics"],
            }
            job.diagnostics = diagnostics
            if blocked:
                job.status = JobStatus.FAILED
                job.error_code = "non_unique_or_illconditioned_solution"
                job.error_message = "QR diagnostic blocked publication; no regularization applied"
                _finish_stage(
                    db,
                    stages["solve"],
                    StageStatus.FAILED,
                    detail,
                    error_code=job.error_code,
                    error_message=job.error_message,
                )
            else:
                _finish_stage(db, stages["solve"], StageStatus.CONFIRMED, detail)
            db.commit()
        except Exception as exc:
            db.rollback()
            job, stages = _load_job(db, job_id)
            detail = {"error": str(exc), "error_type": exc.__class__.__name__}
            _finish_stage(
                db,
                stages["solve"],
                StageStatus.FAILED,
                detail,
                error_code="solve_interrupted",
                error_message=str(exc),
            )
            job.status = JobStatus.AWAITING_RECOVERY
            job.error_code = "solve_interrupted"
            job.error_message = str(exc)
            db.commit()
            raise
        return job_id
    finally:
        db.close()


@celery_app.task(name="pipeline.publish_checks")
def publish_checks(job_id: int) -> int:
    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        if stages["publish_checks"].status == StageStatus.CONFIRMED:
            return job_id
        if stages["solve"].status != StageStatus.CONFIRMED:
            return job_id
        job.current_stage = "publish_checks"
        job.error_code = None
        job.error_message = None
        _begin_stage(stages["publish_checks"])
        db.commit()

        diagnostics = job.diagnostics or {}
        latest_snapshot_id = db.scalar(select(func.max(Snapshot.id)).where(Snapshot.project_id == job.project_id))
        stale_draft = latest_snapshot_id != job.snapshot_id
        checks = {
            "closure": {
                "pre_adjustment_failed": len(diagnostics.get("pre_adjustment_closures", {}).get("failed", [])),
                "post_adjustment_failed": len(diagnostics.get("post_adjustment_closures", {}).get("failed", [])),
                "passed": not diagnostics.get("post_adjustment_closures", {}).get("failed"),
            },
            "datum_constraints": {
                "blocked_components": len(diagnostics.get("blocked_components", [])),
                "passed": not diagnostics.get("blocked_components"),
            },
            "corrections_and_residuals": {
                "statistics": diagnostics.get("residual_statistics", {}),
                "passed": diagnostics.get("residual_statistics", {}).get("count", 0) > 0,
            },
            "snapshot_immutability": {"passed": bool(job.snapshot.immutable)},
            "stale_draft_policy": {
                "latest_snapshot_id": latest_snapshot_id,
                "job_snapshot_id": job.snapshot_id,
                "passed": not stale_draft,
            },
            "regularization": {"value": "none", "passed": diagnostics.get("regularization") == "none"},
            "reproducibility": {
                "observations_sha256": job.snapshot.observations_sha256,
                "rules_sha256": job.snapshot.rules_sha256,
                "algorithm": job.snapshot.algorithm,
                "passed": True,
            },
        }
        passed = all(value.get("passed", False) for value in checks.values())
        failed_checks = sorted(name for name, check in checks.items() if not check.get("passed", False))
        if stale_draft:
            _finish_stage(
                db,
                stages["publish_checks"],
                StageStatus.CONFIRMED,
                checks,
                error_code="stale_snapshot_audit_only",
                error_message="running task completed against an immutable old snapshot; it cannot overwrite the newer draft",
            )
            job.status = JobStatus.AUDITED_ONLY
            job.error_code = "stale_snapshot_audit_only"
            job.error_message = "running task completed against an immutable old snapshot; it cannot overwrite the newer draft"
        elif passed:
            _finish_stage(db, stages["publish_checks"], StageStatus.CONFIRMED, checks)
            job.status = JobStatus.COMPLETED
            job.error_code = None
            job.error_message = None
        else:
            message = f"publication blocked by: {', '.join(failed_checks)}"
            _finish_stage(
                db,
                stages["publish_checks"],
                StageStatus.FAILED,
                checks,
                error_code="publication_checks_failed",
                error_message=message,
            )
            job.status = JobStatus.FAILED
            job.error_code = "publication_checks_failed"
            job.error_message = message
        job.finished_at = _now()
        db.commit()
        return job_id
    finally:
        db.close()


def build_pipeline(job_id: int):
    """Return the idempotent resume request for one job generation."""
    return resume_pipeline_request.s(job_id)


def request_pipeline(job_id: int, client: redis.Redis | None = None) -> bool:
    """Enqueue one orchestrator; repeated resume clicks coalesce on the Redis lock."""
    settings = get_settings()
    redis_client = client or redis.Redis.from_url(settings.redis_url)
    dispatch_lock, run_lock = _lock_names(job_id)
    token = f"request:{uuid4()}"
    acquired = bool(redis_client.set(dispatch_lock, token, nx=True, ex=LOCK_TTL_SECONDS))
    if not acquired or bool(redis_client.exists(run_lock)):
        if acquired:
            redis_client.delete(dispatch_lock)
        return False
    try:
        run_pipeline.apply_async(args=[job_id, token])
    except Exception:
        redis_client.delete(dispatch_lock)
        raise
    return True


@celery_app.task(name="pipeline.request")
def resume_pipeline_request(job_id: int) -> bool:
    return request_pipeline(job_id)


@celery_app.task(name="pipeline.run", bind=True, acks_late=True)
def run_pipeline(self, job_id: int, dispatch_token: str | None = None) -> int:
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url)
    dispatch_lock, lock_name = _lock_names(job_id)
    # Long TTLs are a safety net against hard kills. The normal path releases them.
    if dispatch_token is not None:
        owner = client.get(dispatch_lock)
        if owner is not None and owner.decode() != dispatch_token:
            return job_id
        if owner is None:
            acquired = client.set(lock_name, self.request.id or "pipeline", nx=True, ex=LOCK_TTL_SECONDS)
            if not acquired:
                return job_id
        else:
            transferred = client.renamenx(dispatch_lock, lock_name)
            if not transferred:
                client.delete(dispatch_lock)
                return job_id
    else:
        acquired = client.set(lock_name, self.request.id or "pipeline", nx=True, ex=LOCK_TTL_SECONDS)
        if not acquired:
            return job_id

    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        # If the old worker died, any RUNNING stage has no owner. Retain it as an
        # interrupted attempt, reset to pending, while CONFIRMED stages remain valid.
        for stage in stages.values():
            _mark_interrupted(db, stage)
        if str(job.status) == JobStatus.RUNNING:
            job.status = JobStatus.AWAITING_RECOVERY
        db.commit()

        order = (import_qc, component_precheck, solve, publish_checks)
        for task in order:
            stage_name = task.name.split(".")[-1]
            db.expire_all()
            job, stages = _load_job(db, job_id)
            if stages[stage_name].status == StageStatus.CONFIRMED:
                continue
            try:
                task.apply(args=[job_id], throw=True)
            except Exception as exc:
                db.rollback()
                job, stages = _load_job(db, job_id)
                if str(job.status) == JobStatus.RUNNING:
                    job.status = JobStatus.AWAITING_RECOVERY
                    error_code = f"{stage_name}_interrupted"
                    job.error_code = error_code
                    job.error_message = str(exc)
                    _fail_current_running_stage(db, job, stages, error_code, exc)
                    db.commit()
                raise
            db.expire_all()
            job, stages = _load_job(db, job_id)
            if stages[stage_name].status != StageStatus.CONFIRMED:
                return job_id
        return job_id
    finally:
        client.delete(lock_name)
        if dispatch_token is not None:
            client.delete(dispatch_lock)
        db.close()


@celery_app.task(name="partition.qc")
def qc_partition(rows: list[dict[str, Any]], point_ids: list[int], partition: int) -> dict[str, Any]:
    point_id_set = set(point_ids)
    bad_geometry = [r["line_code"] for r in rows if r["from_point_id"] not in point_id_set or r["to_point_id"] not in point_id_set]
    self_loops = [r["line_code"] for r in rows if r["from_point_id"] == r["to_point_id"]]
    return {
        "partition": partition,
        "row_count": len(rows),
        "bad_geometry": bad_geometry,
        "self_loops": self_loops,
        "ok": not bad_geometry and not self_loops,
    }
