"""Stage-panel recovery acceptance tests.

Covers the three acceptance criteria:
1. A solve-stage failure leaves import_qc/component_precheck confirmed, and a
   resume retries only solve and everything after it.
2. Repeated resume/submit never creates a second job for the same snapshot.
3. Publish-blocking diagnostics stay visible after a restart, and a failed job
   is never reported as a published result.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal, engine
from app.models.schema import Job, JobStage, JobStageAttempt, Publication
from app.services.snapshots import create_immutable_snapshot, ensure_single_generation
from app.workers import tasks

WEIGHT_RULE = {"method": "distance_inverse_km", "c_km": 1.0, "base_sigma_m": 0.001}


def _seed_project(client, code: str, points, observations, datums) -> int:
    project_id = client.post("/api/projects", json={"code": code, "name": code}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/import",
        json={
            "points": [{"code": p} for p in points],
            "observations": [
                {
                    "line_code": line_code,
                    "from_code": a,
                    "to_code": b,
                    "observed_delta_m": delta,
                    "distance_m": distance,
                }
                for line_code, a, b, delta, distance in observations
            ],
        },
    )
    assert response.status_code == 201, response.text
    for point_code, elevation in datums:
        response = client.post(
            f"/api/projects/{project_id}/datums",
            params={"point_code": point_code, "elevation_m": elevation, "sigma_m": 0.0001},
        )
        assert response.status_code == 201, response.text
    response = client.post(f"/api/projects/{project_id}/weight-rules", params={"name": "default"}, json=WEIGHT_RULE)
    assert response.status_code == 201, response.text
    return project_id


def _good_network(client) -> int:
    # Zero-misclosure triangle anchored by one datum: solve must succeed.
    return _seed_project(
        client,
        "GOOD",
        ["A", "B", "C"],
        [
            ("L1", "A", "B", 1.0, 1000.0),
            ("L2", "B", "C", 1.0, 1000.0),
            ("L3", "A", "C", 2.0, 2000.0),
        ],
        [("A", 100.0)],
    )


def _make_job(db, project_id: int) -> Job:
    snapshot = create_immutable_snapshot(db, project_id)
    job, created = ensure_single_generation(db, project_id, snapshot.id)
    assert created
    db.commit()
    return job


def _stage(detail: dict, name: str) -> dict:
    return next(s for s in detail["stages"] if s["name"] == name)


def _job_count(db) -> int:
    return db.scalar(select(func.count(Job.id)))


def test_solve_failure_resume_retries_only_solve_and_after(client, db, eager_pipeline):
    """Acceptance 1: confirmed stages stay untouched; resume retries solve+."""
    project_id = _good_network(client)
    job = _make_job(db, project_id)

    def boom(_db, _job):
        raise RuntimeError("simulated solve crash")

    original = tasks.execute_solve
    tasks.execute_solve = boom
    try:
        with pytest.raises(RuntimeError, match="simulated solve crash"):
            tasks.run_pipeline.apply(args=[job.id], throw=True)
    finally:
        tasks.execute_solve = original

    detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["status"] == "awaiting_recovery"
    assert detail["recovery_point"] == "solve"
    assert detail["published"] is False

    import_qc = _stage(detail, "import_qc")
    precheck = _stage(detail, "component_precheck")
    solve = _stage(detail, "solve")
    assert import_qc["status"] == "confirmed" and import_qc["confirmed_at"]
    assert precheck["status"] == "confirmed" and precheck["confirmed_at"]
    assert solve["status"] == "failed"
    assert solve["last_attempt"]["status"] == "failed"
    assert solve["last_attempt"]["error_code"] == "solve_interrupted"
    assert "simulated solve crash" in solve["last_attempt"]["error_message"]

    # Resume: only solve and publish_checks may be retried.
    resumed = client.post(f"/api/jobs/{job.id}/resume").json()
    assert resumed["recovery_point"] == "solve"
    assert resumed["skipped_confirmed"] == ["import_qc", "component_precheck"]
    assert resumed["retry_stages"] == ["solve", "publish_checks"]
    assert resumed["enqueued"] is True

    detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["status"] == "completed"
    assert detail["recovery_point"] is None

    # Confirmed stages were never re-run: same attempt counter and confirm time.
    assert _stage(detail, "import_qc")["attempt"] == 1
    assert _stage(detail, "component_precheck")["attempt"] == 1
    assert _stage(detail, "import_qc")["confirmed_at"] == import_qc["confirmed_at"]
    assert _stage(detail, "component_precheck")["confirmed_at"] == precheck["confirmed_at"]

    # The failed solve attempt is preserved for audit next to the retry.
    assert _stage(detail, "solve")["attempt"] == 2
    attempts = client.get(f"/api/jobs/{job.id}/stage-attempts").json()
    solve_attempts = [a for a in attempts if a["stage_name"] == "solve"]
    assert [(a["attempt"], a["status"]) for a in solve_attempts] == [(1, "failed"), (2, "confirmed")]
    assert "simulated solve crash" in solve_attempts[0]["error_message"]
    assert [a["stage_name"] for a in attempts if a["status"] == "confirmed"] == [
        "import_qc",
        "component_precheck",
        "solve",
        "publish_checks",
    ]


def test_repeated_submit_and_resume_never_fork_generation(client, db, eager_pipeline):
    """Acceptance 2: one snapshot maps to exactly one job, however often asked."""
    project_id = _good_network(client)

    first = client.post(f"/api/projects/{project_id}/jobs").json()
    second = client.post(f"/api/projects/{project_id}/jobs").json()
    assert first["job_id"] == second["job_id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert _job_count(db) == 1

    detail = client.get(f"/api/jobs/{first['job_id']}").json()
    assert detail["status"] == "completed"

    # Repeated resume on a fully confirmed job is a no-op: nothing re-runs and
    # no second job appears for the same snapshot.
    attempts_before = client.get(f"/api/jobs/{first['job_id']}/stage-attempts").json()
    for _ in range(2):
        resumed = client.post(f"/api/jobs/{first['job_id']}/resume").json()
        assert resumed["enqueued"] is False
        assert resumed["deduplicated"] is True
        assert resumed["retry_stages"] == []
    assert _job_count(db) == 1
    assert client.get(f"/api/jobs/{first['job_id']}/stage-attempts").json() == attempts_before
    assert client.get(f"/api/jobs/{first['job_id']}").json()["status"] == "completed"


def test_publish_blocking_diagnostics_survive_restart(client, db, eager_pipeline):
    """Acceptance 3: blocked-publish diagnostics persist; failed != published."""
    # Component D-E has no datum: QR diagnosis must block, never regularize.
    project_id = _seed_project(
        client,
        "BLOCKED",
        ["A", "B", "D", "E"],
        [
            ("L1", "A", "B", 1.0, 1000.0),
            ("L2", "D", "E", 2.0, 1000.0),
        ],
        [("A", 100.0)],
    )
    submitted = client.post(f"/api/projects/{project_id}/jobs").json()
    job_id = submitted["job_id"]

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "non_unique_or_illconditioned_solution"
    assert len(detail["diagnostics"]["blocked_components"]) == 1
    assert detail["diagnostics"]["regularization"] == "none"

    # Simulate a full restart: drop every connection and re-read from disk.
    SessionLocal.close_all()
    engine.dispose()
    db.close()

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "non_unique_or_illconditioned_solution"
    assert len(detail["diagnostics"]["blocked_components"]) == 1
    assert detail["recovery_point"] == "solve"
    solve = _stage(detail, "solve")
    assert solve["status"] == "failed"
    assert solve["last_attempt"]["error_code"] == "non_unique_or_illconditioned_solution"
    assert _stage(detail, "import_qc")["status"] == "confirmed"
    assert _stage(detail, "component_precheck")["status"] == "confirmed"

    attempts = client.get(f"/api/jobs/{job_id}/stage-attempts").json()
    assert [(a["stage_name"], a["status"]) for a in attempts] == [
        ("import_qc", "confirmed"),
        ("component_precheck", "confirmed"),
        ("solve", "failed"),
    ]

    # Retrying the failed stage appends new attempts; the old failure stays.
    resumed = client.post(f"/api/jobs/{job_id}/resume").json()
    assert resumed["skipped_confirmed"] == ["import_qc", "component_precheck"]
    assert resumed["retry_stages"] == ["solve", "publish_checks"]
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert _stage(detail, "solve")["attempt"] == 2
    assert _stage(detail, "import_qc")["attempt"] == 1
    solve_attempts = [
        a for a in client.get(f"/api/jobs/{job_id}/stage-attempts").json() if a["stage_name"] == "solve"
    ]
    assert [(a["attempt"], a["status"]) for a in solve_attempts] == [(1, "failed"), (2, "failed")]
    assert all(a["error_code"] == "non_unique_or_illconditioned_solution" for a in solve_attempts)

    # A failed job can never be published nor presented as published.
    assert detail["published"] is False
    response = client.post(f"/api/jobs/{job_id}/publish", json={"confirm": True})
    assert response.status_code == 409
    assert client.get(f"/api/jobs/{job_id}").json()["published"] is False
    fresh = SessionLocal()
    try:
        assert fresh.scalar(select(func.count(Publication.id)).where(Publication.job_id == job_id)) == 0
    finally:
        fresh.close()


def test_interrupted_running_stage_is_audited_before_reset(client, db, eager_pipeline):
    """A killed worker's RUNNING marker becomes an interrupted attempt record."""
    project_id = _good_network(client)
    job = _make_job(db, project_id)

    stage = db.scalar(select(JobStage).where(JobStage.job_id == job.id, JobStage.name == "solve"))
    stage.status = "running"
    stage.attempt = 1
    stage.started_at = tasks._now()
    job.status = "running"
    db.commit()

    tasks.run_pipeline.apply(args=[job.id])

    detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["status"] == "completed"
    attempts = client.get(f"/api/jobs/{job.id}/stage-attempts").json()
    solve_attempts = [a for a in attempts if a["stage_name"] == "solve"]
    assert [(a["attempt"], a["status"]) for a in solve_attempts] == [(1, "interrupted"), (2, "confirmed")]
    assert "worker lost" in solve_attempts[0]["detail"]["reason"]


def test_confirmed_stage_is_never_rerun_even_when_pipeline_restarts(client, db, eager_pipeline):
    """Invariant: re-entering the pipeline skips confirmed stages entirely."""
    project_id = _good_network(client)
    job = _make_job(db, project_id)

    tasks.run_pipeline.apply(args=[job.id])
    first = client.get(f"/api/jobs/{job.id}").json()
    assert first["status"] == "completed"
    confirmed_at = {s["name"]: s["confirmed_at"] for s in first["stages"]}

    # Re-enter the pipeline (e.g. worker reboot with a duplicate message).
    tasks.run_pipeline.apply(args=[job.id])
    second = client.get(f"/api/jobs/{job.id}").json()
    assert {s["name"]: s["confirmed_at"] for s in second["stages"]} == confirmed_at
    assert all(s["attempt"] == 1 for s in second["stages"])
    attempts = db.scalar(select(func.count(JobStageAttempt.id)).where(JobStageAttempt.job_id == job.id))
    assert attempts == 4
