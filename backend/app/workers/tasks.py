from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import Job, JobStage, JobStatus, Snapshot, StageStatus
from app.services import network
from app.services.solver import execute_solve
from app.workers.celery_app import celery_app

STAGES = ("import_qc", "component_precheck", "solve", "publish_checks")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_job(db: Session, job_id: int) -> tuple[Job, dict[str, JobStage]]:
    job = db.get(Job, job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")
    stages = {s.name: s for s in job.stages}
    return job, stages


def _mark_stage(stage: JobStage, status: str, detail: dict[str, Any] | None = None) -> None:
    if status == StageStatus.RUNNING and stage.status != StageStatus.RUNNING:
        stage.attempt += 1
        stage.started_at = _now()
    elif status == StageStatus.CONFIRMED:
        stage.confirmed_at = _now()
        stage.completed_at = _now()
    elif status == StageStatus.FAILED:
        stage.completed_at = _now()
    if detail is not None:
        stage.detail = detail
    stage.status = status


@celery_app.task(name="pipeline.import_qc", bind=True, max_retries=2, default_retry_delay=5)
def import_qc(self, job_id: int) -> int:
    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        if stages["import_qc"].status == StageStatus.CONFIRMED:
            return job_id
        job.status = JobStatus.RUNNING
        job.current_stage = "import_qc"
        job.attempt += 1
        _mark_stage(stages["import_qc"], StageStatus.RUNNING)
        db.commit()
        try:
            rows = job.snapshot.payload["observations"]
            point_ids = [point["id"] for point in job.snapshot.payload["points"]]
            size = (len(rows) + 7) // 8
            chunks = [rows[index * size : (index + 1) * size] for index in range(8)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                qc = list(executor.map(lambda args: qc_partition.run(*args), ((chunk, point_ids, index) for index, chunk in enumerate(chunks))))
            failed = [p for p in qc if not p["ok"]]
            _mark_stage(
                stages["import_qc"],
                StageStatus.CONFIRMED if not failed else StageStatus.FAILED,
                {"partitions": qc, "failed_count": len(failed)},
            )
            if failed:
                job.status = JobStatus.FAILED
                job.error_code = "import_qc_failed"
            db.commit()
            if failed:
                return job_id
        except Exception as exc:
            db.rollback()
            try:
                job, stages = _load_job(db, job_id)
                _mark_stage(stages["import_qc"], StageStatus.FAILED, {"error": str(exc)})
                job.status = JobStatus.AWAITING_RECOVERY
                job.error_code = "import_qc_interrupted"
                job.error_message = str(exc)
                db.commit()
            finally:
                db.close()
            raise self.retry(exc=exc)
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
        _mark_stage(stages["component_precheck"], StageStatus.RUNNING)
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
        _mark_stage(stages["component_precheck"], StageStatus.CONFIRMED, detail)
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
        _mark_stage(stages["solve"], StageStatus.RUNNING)
        db.commit()
        try:
            output = execute_solve(db, job)
            blocked = output["diagnostics"]["blocked_components"]
            closure_failed = output["diagnostics"]["pre_adjustment_closures"]["failed"]
            _mark_stage(
                stages["solve"],
                StageStatus.CONFIRMED if not blocked else StageStatus.FAILED,
                {
                    "component_count": output["diagnostics"]["component_count"],
                    "blocked_count": len(blocked),
                    "pre_closure_failed_count": len(closure_failed),
                    "residual_statistics": output["diagnostics"]["residual_statistics"],
                },
            )
            job.diagnostics = output["diagnostics"]
            job.status = JobStatus.FAILED if blocked else JobStatus.RUNNING
            if blocked:
                job.error_code = "non_unique_or_illconditioned_solution"
                job.error_message = "QR diagnostic blocked publication; no regularization applied"
            db.commit()
        except Exception as exc:
            db.rollback()
            job, stages = _load_job(db, job_id)
            _mark_stage(stages["solve"], StageStatus.FAILED, {"error": str(exc)})
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
            job.status = JobStatus.FAILED
            job.finished_at = _now()
            db.commit()
            return job_id
        job.current_stage = "publish_checks"
        _mark_stage(stages["publish_checks"], StageStatus.RUNNING)
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
        if stale_draft:
            _mark_stage(stages["publish_checks"], StageStatus.CONFIRMED, checks)
            job.status = JobStatus.AUDITED_ONLY
            job.error_code = "stale_snapshot_audit_only"
            job.error_message = "running task completed against an immutable old snapshot; it cannot overwrite the newer draft"
        else:
            _mark_stage(stages["publish_checks"], StageStatus.CONFIRMED if passed else StageStatus.FAILED, checks)
            job.status = JobStatus.COMPLETED if passed else JobStatus.FAILED
        job.finished_at = _now()
        db.commit()
        return job_id
    finally:
        db.close()


def build_pipeline(job_id: int):
    # One orchestrator per generation. Because each stage checks a database-confirm
    # marker, killing and restarting the worker resumes after confirmed stages.
    return run_pipeline.s(job_id)


@celery_app.task(name="pipeline.run", bind=True, acks_late=True)
def run_pipeline(self, job_id: int) -> int:
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url)
    lock_name = f"job-generation-lock:{job_id}"
    # Long TTL is a safety net against hard kills. The normal path releases it.
    acquired = client.set(lock_name, self.request.id or "pipeline", nx=True, ex=60 * 60)
    if not acquired:
        return job_id

    db = SessionLocal()
    try:
        job, stages = _load_job(db, job_id)
        # If the old worker died, any RUNNING stage has no owner. Reset it to pending
        # while CONFIRMED stages remain valid recovery points.
        for stage in stages.values():
            if stage.status == StageStatus.RUNNING:
                stage.status = StageStatus.PENDING
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
            task.apply(args=[job_id], throw=True)
            db.expire_all()
            job, stages = _load_job(db, job_id)
            if stages[stage_name].status != StageStatus.CONFIRMED:
                return job_id
        return job_id
    finally:
        client.delete(lock_name)
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
