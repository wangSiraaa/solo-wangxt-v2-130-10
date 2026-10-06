from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectIn(BaseModel):
    code: str
    name: str


class PointIn(BaseModel):
    code: str
    name: str | None = None
    lock_version: int = 1


class ObservationIn(BaseModel):
    line_code: str
    from_code: str
    to_code: str
    observed_delta_m: float
    distance_m: float = Field(gt=0)
    direction: Literal["forward", "backward", "mean"] = "forward"
    pair_group: str | None = None
    weight_override: float | None = None


class BulkImportIn(BaseModel):
    points: list[PointIn]
    observations: list[ObservationIn]


class OptimisticObservationPatch(BaseModel):
    lock_version: int
    observed_delta_m: float | None = None
    distance_m: float | None = Field(default=None, gt=0)
    weight_override: float | None = None
    active: bool | None = None


class OptimisticDatumPatch(BaseModel):
    lock_version: int
    elevation_m: float | None = None
    sigma_m: float | None = Field(default=None, gt=0)
    active: bool | None = None


class OptimisticRulePatch(BaseModel):
    lock_version: int
    rule: dict[str, Any] | None = None
    active: bool | None = None


class SubmitJobIn(BaseModel):
    resume: bool = False


class PublishIn(BaseModel):
    confirm: bool = True
