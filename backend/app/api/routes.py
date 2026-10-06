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
from app.workers.tasks import build_pipeline

router = APIRouter(prefix="/api")


def _get(db: Session, model, entity_id: int):
    obj = db.get(model, entity_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {entity_id} not found")
    return obj


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
        build_pipeline(job.id).apply_async()
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
    # Confirmed stages are skipped. Failed stages are retried; /resume also resets
    # RUNNING markers left by a killed worker.
    pipeline = build_pipeline(job.id)
    pipeline.apply_async()
    return {"job_id": job.id, "current_stage": job.current_stage, "confirmed": [n for n, s in stages.items() if s.status == "confirmed"]}


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
    return {
        "id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "generation_key": job.generation_key,
        "snapshot_version": job.snapshot.version,
        "input_summary": job.snapshot.input_summary,
        "algorithm": job.snapshot.algorithm,
        "diagnostics": job.diagnostics,
        "stages": [
            {"name": s.name, "status": s.status, "attempt": s.attempt, "detail": s.detail}
            for s in sorted(db.scalars(select(JobStage).where(JobStage.job_id == job_id)).all(), key=lambda s: s.id)
        ],
    }


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
