from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    BulkImportIn,
    OptimisticDatumPatch,
    OptimisticObservationPatch,
    OptimisticRulePatch,
    ProjectIn,
    PublishIn,
)
from app.core.db import get_db
from app.models.schema import (
    Datum,
    Job,
    JobStage,
    Observation,
    Point,
    Project,
    Publication,
    Snapshot,
    WeightRule,
    ObservationResult,
    ComponentResult,
)
from app.services.snapshots import apply_optimistic_update, create_immutable_snapshot, ensure_single_generation
from app.workers.tasks import STAGES, request_pipeline

router = APIRouter(prefix="/api")


def _get(db: Session, model, entity_id: int):
    obj = db.get(model, entity_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {entity_id} not found")
    return obj


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def _serialize_attempt(attempt) -> dict:
    return {
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "detail": attempt.detail,
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "started_at": _iso(attempt.started_at),
        "finished_at": _iso(attempt.finished_at),
        "created_at": _iso(attempt.created_at),
    }


def _serialize_stage(stage: JobStage) -> dict:
    attempts = list(stage.attempts)
    latest = attempts[-1] if attempts else None
    if latest is not None:
        latest_payload = _serialize_attempt(latest)
        latest_started = latest.started_at
        latest_finished = latest.finished_at or latest.started_at
    else:
        latest_payload = {
            "attempt_number": stage.attempt,
            "status": stage.status,
            "detail": stage.detail,
            "error_code": None,
            "error_message": None,
            "started_at": _iso(stage.started_at),
            "finished_at": _iso(stage.completed_at),
            "created_at": None,
        }
        latest_started = stage.started_at
        latest_finished = stage.completed_at or stage.started_at

    failure_diagnostic = None
    if latest is not None and latest.status != "confirmed":
        failure_diagnostic = {
            "detail": latest.detail,
            "error_code": latest.error_code,
            "error_message": latest.error_message,
        }
    elif latest is None and stage.status == "failed":
        failure_diagnostic = {"detail": stage.detail, "error_code": None, "error_message": None}

    return {
        "name": stage.name,
        "status": stage.status,
        "attempt": stage.attempt,
        "retry_count": max(0, (len(attempts) if attempts else stage.attempt) - 1),
        "detail": stage.detail,
        "started_at": _iso(stage.started_at),
        "confirmed_at": _iso(stage.confirmed_at),
        "completed_at": _iso(stage.completed_at),
        "latest_attempt": latest_payload,
        "latest_attempt_at": _iso(latest_finished or latest_started),
        "failure_diagnostic": failure_diagnostic,
        "attempts": [_serialize_attempt(attempt) for attempt in attempts],
    }


def _recovery_point(stages: dict[str, JobStage]) -> str | None:
    for name in STAGES:
        stage = stages.get(name)
        if stage is not None and stage.status != "confirmed":
            return name
    return None


def _publication_blockers(job: Job, stages: dict[str, JobStage]) -> list[str]:
    blockers: list[str] = []
    if str(job.status) == "audited_only":
        blockers.append("旧快照任务只能审计；请基于当前草稿创建新快照后发布。")
    elif str(job.status) != "completed":
        recovery_stage = _recovery_point(stages)
        if recovery_stage:
            blockers.append(f"任务尚未通过 {recovery_stage}，当前不是可发布成果。")
        else:
            blockers.append(f"任务状态为 {job.status}，当前不是可发布成果。")

    for name in STAGES:
        stage = stages.get(name)
        if stage is None:
            continue
        diagnostic = stage.attempts[-1] if stage.attempts else None
        if stage.status == "failed":
            message = diagnostic.error_message if diagnostic is not None else None
            blockers.append(f"{name} 未通过：{message or '请查看阶段诊断'}")
        elif name == "publish_checks" and stage.status == "confirmed":
            for check_name, check in stage.detail.items():
                if isinstance(check, dict) and not check.get("passed", False):
                    blockers.append(f"发布核对 {check_name} 未通过")
    if job.error_code:
        blockers.append(f"{job.error_code}: {job.error_message or '需要恢复后才能发布'}")
    return list(dict.fromkeys(blockers))


def _serialize_job(db: Session, job: Job) -> dict:
    stages = {stage.name: stage for stage in job.stages}
    recovery_stage = _recovery_point(stages)
    has_publication = bool(
        db.scalar(select(Publication.id).where(Publication.job_id == job.id).limit(1))
    )
    blockers = _publication_blockers(job, stages)
    return {
        "id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "generation_key": job.generation_key,
        "snapshot_version": job.snapshot.version,
        "input_summary": job.snapshot.input_summary,
        "algorithm": job.snapshot.algorithm,
        "diagnostics": job.diagnostics,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "recovery_stage": recovery_stage,
        "confirmed_stages": [name for name in STAGES if (stages.get(name) and stages[name].status == "confirmed")],
        "can_resume": str(job.status) in {"failed", "awaiting_recovery", "pending"} and recovery_stage is not None,
        "can_publish": str(job.status) == "completed" and not blockers,
        "publication_blockers": blockers,
        "has_publication": has_publication,
        "stages": [_serialize_stage(stages[name]) for name in STAGES if name in stages],
    }


@router.post("/projects", status_code=201)
def create_project(payload: ProjectIn, db: Session = Depends(get_db)):
    project = Project(code=payload.code, name=payload.name)
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"project code {payload.code} already exists") from None
    db.refresh(project)
    return {"id": project.id, "code": project.code, "lock_version": project.lock_version}


@router.post("/projects/{project_id}/import", status_code=201)
def bulk_import(project_id: int, payload: BulkImportIn, db: Session = Depends(get_db)):
    project = _get(db, Project, project_id)
    existing = {p.code: p for p in db.scalars(select(Point).where(Point.project_id == project_id)).all()}
    created_points = 0
    for point in payload.points:
        if point.code not in existing:
            instance = Point(project_id=project_id, code=point.code, name=point.name)
            db.add(instance)
            created_points += 1
    db.flush()
    points = {p.code: p for p in db.scalars(select(Point).where(Point.project_id == project_id)).all()}
    missing = sorted(
        {obs.from_code for obs in payload.observations} | {obs.to_code for obs in payload.observations} - set(points)
    )
    if missing:
        raise HTTPException(400, f"unknown point codes in observations: {missing[:20]}")
    observations = []
    for obs in payload.observations:
        observations.append(
            Observation(
                project_id=project_id,
                line_code=obs.line_code,
                from_point_id=points[obs.from_code].id,
                to_point_id=points[obs.to_code].id,
                observed_delta_m=obs.observed_delta_m,
                distance_m=obs.distance_m,
                direction=obs.direction,
                pair_group=obs.pair_group,
                weight_override=obs.weight_override,
            )
        )
    db.add_all(observations)
    project.lock_version += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "one or more line codes already exist in this project") from None
    return {"points_created": created_points, "observations_created": len(observations)}


@router.post("/projects/{project_id}/datums", status_code=201)
def add_datum(project_id: int, point_code: str, elevation_m: float, sigma_m: float = 0.001, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    point = db.scalar(select(Point).where(Point.project_id == project_id, Point.code == point_code))
    if point is None:
        raise HTTPException(404, f"point {point_code} not found")
    datum = Datum(project_id=project_id, point_id=point.id, elevation_m=elevation_m, sigma_m=sigma_m)
    db.add(datum)
    db.commit()
    return {"id": datum.id, "lock_version": datum.lock_version}


@router.post("/projects/{project_id}/weight-rules", status_code=201)
def add_weight_rule(project_id: int, name: str, rule: dict, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    model = WeightRule(project_id=project_id, name=name, rule=rule)
    db.add(model)
    db.commit()
    return {"id": model.id, "lock_version": model.lock_version}


@router.patch("/observations/{observation_id}")
def patch_observation(observation_id: int, payload: OptimisticObservationPatch, db: Session = Depends(get_db)):
    obs = _get(db, Observation, observation_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, obs, changes, expected_version=payload.lock_version)
    db.commit()
    return {"id": obs.id, "lock_version": obs.lock_version}


@router.patch("/datums/{datum_id}")
def patch_datum(datum_id: int, payload: OptimisticDatumPatch, db: Session = Depends(get_db)):
    datum = _get(db, Datum, datum_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, datum, changes, expected_version=payload.lock_version)
    db.commit()
    return {"id": datum.id, "lock_version": datum.lock_version}


@router.patch("/weight-rules/{rule_id}")
def patch_weight_rule(rule_id: int, payload: OptimisticRulePatch, db: Session = Depends(get_db)):
    rule = _get(db, WeightRule, rule_id)
    changes = payload.model_dump(exclude={"lock_version"}, exclude_none=True)
    apply_optimistic_update(db, rule, changes, expected_version=payload.lock_version)
    db.commit()
    return {"id": rule.id, "lock_version": rule.lock_version}


@router.post("/projects/{project_id}/jobs", status_code=202)
def submit_job(project_id: int, db: Session = Depends(get_db)):
    _get(db, Project, project_id)
    snapshot = create_immutable_snapshot(db, project_id)
    job, created = ensure_single_generation(db, project_id, snapshot.id)
    db.commit()
    if created:
        request_pipeline(job.id)
    return {
        "job_id": job.id,
        "snapshot_id": snapshot.id,
        "snapshot_version": snapshot.version,
        "generation_key": job.generation_key,
        "deduplicated": not created,
    }


@router.post("/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    stages = {s.name: s for s in job.stages}
    recovery_stage = _recovery_point(stages)
    if recovery_stage is None:
        raise HTTPException(409, "all stages are already confirmed; resume cannot rerun this job")
    if str(job.status) not in {"failed", "awaiting_recovery", "pending"}:
        raise HTTPException(409, f"job {job.status} cannot be resumed")

    enqueued = request_pipeline(job.id)
    return {
        "job_id": job.id,
        "recovery_stage": recovery_stage,
        "confirmed": [name for name, stage in stages.items() if stage.status == "confirmed"],
        "enqueued": enqueued,
        "deduplicated": not enqueued,
    }


@router.get("/projects/{project_id}/topology")
def topology(project_id: int, db: Session = Depends(get_db)):
    points = db.scalars(select(Point).where(Point.project_id == project_id).order_by(Point.id)).all()
    obs = db.scalars(select(Observation).where(Observation.project_id == project_id).order_by(Observation.id)).all()
    point_index = {p.id: p.code for p in points}
    return {
        "nodes": [{"data": {"id": p.code, "label": p.code, "point_id": p.id}} for p in points],
        "edges": [
            {
                "data": {
                    "id": o.line_code,
                    "source": point_index[o.from_point_id],
                    "target": point_index[o.to_point_id],
                    "label": f"{float(o.observed_delta_m):.3f}",
                }
            }
            for o in obs
        ],
    }


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    return _serialize_job(db, job)


@router.get("/jobs/{job_id}/residuals")
def residuals(job_id: int, limit: int = 200, db: Session = Depends(get_db)):
    _get(db, Job, job_id)
    results = db.scalars(select(ObservationResult).where(ObservationResult.job_id == job_id).limit(limit)).all()
    return [
        {
            "line_code": r.line_code,
            "observed_delta_m": float(r.observed_delta_m),
            "adjusted_delta_m": None if r.adjusted_delta_m is None else float(r.adjusted_delta_m),
            "correction_m": None if r.correction_m is None else float(r.correction_m),
            "residual": None if r.residual_v is None else float(r.residual_v),
        }
        for r in results
    ]


@router.post("/jobs/{job_id}/publish", status_code=201)
def publish(job_id: int, payload: PublishIn, db: Session = Depends(get_db)):
    job = _get(db, Job, job_id)
    if not payload.confirm:
        raise HTTPException(400, "publication requires confirm=true")
    if str(job.status) != "completed":
        raise HTTPException(409, f"job is not publishable: {job.status}")
    stage_checks = {s.name: s.detail for s in db.scalars(select(JobStage).where(JobStage.job_id == job_id)).all()}
    if stage_checks.get("publish_checks", {}).get("regularization", {}).get("value") != "none":
        raise HTTPException(409, "regularization audit failed")

    current_snapshot_id = db.scalar(
        select(func.max(Snapshot.id)).where(Snapshot.project_id == job.project_id)
    )
    if current_snapshot_id != job.snapshot_id:
        # Old task may finish as audit history, but cannot overwrite a newer surveyor draft.
        raise HTTPException(409, "stale generation: rerun against current draft before publishing")

    elevations: dict[str, float] = {}
    for component in db.scalars(select(ComponentResult).where(ComponentResult.job_id == job_id)).all():
        if component.status != "ok":
            raise HTTPException(409, f"component {component.component_index} is {component.status}")
        elevations.update(component.elevations)
    # Component elevations key by point id; expose stable codes.
    points = {p.id: p.code for p in db.scalars(select(Point).where(Point.project_id == job.project_id)).all()}
    elevations = {points.get(int(pid), str(pid)): value for pid, value in elevations.items()}

    version = (db.scalar(select(func.max(Publication.version)).where(Publication.project_id == job.project_id)) or 0) + 1
    publication = Publication(
        project_id=job.project_id,
        job_id=job.id,
        snapshot_id=job.snapshot_id,
        version=version,
        checks=stage_checks,
        input_summary=job.snapshot.input_summary,
        algorithm=job.snapshot.algorithm,
        elevations=elevations,
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return {"publication_id": publication.id, "version": publication.version, "elevation_count": len(elevations)}
