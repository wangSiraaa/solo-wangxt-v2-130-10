"""Stage recovery-point helpers shared by the API and the pipeline workers."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.models.schema import JobStage, JobStageAttempt, StageStatus

STAGE_ORDER = ("import_qc", "component_precheck", "solve", "publish_checks")


def recovery_point(stages: Mapping[str, JobStage]) -> str | None:
    """First stage that is not confirmed; resume continues from there.

    Confirmed stages are never re-run. Returns None when every stage is
    confirmed (nothing left to recover).
    """
    for name in STAGE_ORDER:
        stage = stages.get(name)
        if stage is None or stage.status != StageStatus.CONFIRMED:
            return name
    return None


def retry_stages(stages: Mapping[str, JobStage]) -> list[str]:
    """Stages a resume would (re)execute: everything not yet confirmed."""
    return [name for name in STAGE_ORDER if stages.get(name) is None or stages[name].status != StageStatus.CONFIRMED]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_attempt(attempt: JobStageAttempt) -> dict[str, Any]:
    return {
        "stage_name": attempt.stage_name,
        "attempt": attempt.attempt,
        "status": attempt.status,
        "detail": attempt.detail,
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "started_at": _iso(attempt.started_at),
        "completed_at": _iso(attempt.completed_at),
    }


def serialize_stage(stage: JobStage, attempts: list[JobStageAttempt]) -> dict[str, Any]:
    return {
        "name": stage.name,
        "status": stage.status,
        "attempt": stage.attempt,
        "detail": stage.detail,
        "started_at": _iso(stage.started_at),
        "confirmed_at": _iso(stage.confirmed_at),
        "completed_at": _iso(stage.completed_at),
        "attempts_recorded": len(attempts),
        "last_attempt": serialize_attempt(attempts[-1]) if attempts else None,
    }
