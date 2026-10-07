from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.routes import _serialize_job
from app.workers import tasks


def _stage_attempt(number, status, *, code=None, message=None, detail=None):
    started = datetime(2026, 10, 7, 10, number, tzinfo=timezone.utc)
    finished = datetime(2026, 10, 7, 10, number + 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        attempt_number=number,
        status=status,
        detail=detail or {},
        error_code=code,
        error_message=message,
        started_at=started,
        finished_at=finished,
        created_at=finished,
    )


def _stage(name, status, attempt, attempts, *, confirmed_at=None, detail=None):
    return SimpleNamespace(
        name=name,
        status=status,
        attempt=attempt,
        attempts=attempts,
        detail=detail or {},
        started_at=attempts[-1].started_at if attempts else None,
        confirmed_at=confirmed_at,
        completed_at=attempts[-1].finished_at if attempts else None,
    )


def test_job_serializer_marks_solve_as_recovery_point_and_keeps_attempt_history():
    confirmed_time = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
    stages = {
        "import_qc": _stage(
            "import_qc",
            "confirmed",
            1,
            [_stage_attempt(1, "confirmed", detail={"failed_count": 0})],
            confirmed_at=confirmed_time,
        ),
        "component_precheck": _stage(
            "component_precheck",
            "confirmed",
            1,
            [_stage_attempt(1, "confirmed", detail={"components": []})],
            confirmed_at=confirmed_time,
        ),
        "solve": _stage(
            "solve",
            "failed",
            2,
            [
                _stage_attempt(
                    1,
                    "failed",
                    code="solve_interrupted",
                    message="simulated solver failure",
                    detail={"error": "simulated solver failure"},
                ),
                _stage_attempt(
                    2,
                    "failed",
                    code="non_unique_or_illconditioned_solution",
                    message="QR diagnostic blocked publication; no regularization applied",
                    detail={"blocked_components": [{"component": 0}]},
                ),
            ],
            detail={"blocked_components": [{"component": 0}]},
        ),
        "publish_checks": _stage("publish_checks", "pending", 0, []),
    }
    job = SimpleNamespace(
        id=7,
        status="failed",
        current_stage="solve",
        generation_key="project:1:snapshot:3",
        snapshot=SimpleNamespace(version=3, input_summary={}, algorithm={}),
        diagnostics={"blocked_components": [{"component": 0}]},
        error_code="non_unique_or_illconditioned_solution",
        error_message="QR diagnostic blocked publication; no regularization applied",
        stages=list(stages.values()),
    )
    db = SimpleNamespace(scalar=lambda _query: None)

    payload = _serialize_job(db, job)

    assert payload["recovery_stage"] == "solve"
    assert payload["confirmed_stages"] == ["import_qc", "component_precheck"]
    assert payload["can_resume"] is True
    assert payload["can_publish"] is False
    assert any("当前不是可发布成果" in blocker for blocker in payload["publication_blockers"])
    solve = next(stage for stage in payload["stages"] if stage["name"] == "solve")
    assert solve["retry_count"] == 1
    assert solve["failure_diagnostic"]["error_code"] == "non_unique_or_illconditioned_solution"
    assert [attempt["attempt_number"] for attempt in solve["attempts"]] == [1, 2]
    assert solve["attempts"][0]["error_message"] == "simulated solver failure"
    assert payload["stages"][0]["confirmed_at"] is not None


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value.encode() if isinstance(value, str) else value
        return True

    def exists(self, key):
        return int(key in self.values)

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)


def test_duplicate_resume_requests_coalesce_without_another_job(monkeypatch):
    fake_redis = FakeRedis()
    enqueued = []

    def apply_async(args, **kwargs):
        enqueued.append(args)

    monkeypatch.setattr(tasks.run_pipeline, "apply_async", apply_async)

    assert tasks.request_pipeline(42, client=fake_redis) is True
    assert tasks.request_pipeline(42, client=fake_redis) is False
    assert len(enqueued) == 1
    assert enqueued[0][0] == 42
