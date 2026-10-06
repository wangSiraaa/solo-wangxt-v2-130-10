from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_RECOVERY = "awaiting_recovery"
    COMPLETED = "completed"
    FAILED = "failed"
    AUDITED_ONLY = "audited_only"


class StageStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    FAILED = "failed"
    SKIPPED = "skipped"


class SnapshotKind(StrEnum):
    OBSERVATIONS_RULES = "observations_rules"
    PUBLICATION = "publication"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    points = relationship("Point", back_populates="project", cascade="all, delete-orphan")
    observations = relationship("Observation", back_populates="project", cascade="all, delete-orphan")
    datums = relationship("Datum", back_populates="project", cascade="all, delete-orphan")
    weight_rules = relationship("WeightRule", back_populates="project", cascade="all, delete-orphan")


class Point(Base):
    __tablename__ = "points"
    __table_args__ = (
        UniqueConstraint("project_id", "code", name="uq_point_project_code"),
        Index("ix_points_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    geom = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    project = relationship("Project", back_populates="points")


class Observation(Base):
    """Draft observation. The observed height difference is never overwritten by adjustment."""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("project_id", "line_code", name="uq_observation_project_line"),
        Index("ix_obs_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    line_code: Mapped[str] = mapped_column(String(80), nullable=False)
    from_point_id: Mapped[int] = mapped_column(ForeignKey("points.id", ondelete="RESTRICT"), nullable=False)
    to_point_id: Mapped[int] = mapped_column(ForeignKey("points.id", ondelete="RESTRICT"), nullable=False)
    observed_delta_m: Mapped[float] = mapped_column(Numeric(18, 9), nullable=False)
    distance_m: Mapped[float] = mapped_column(Numeric(15, 3), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="forward", nullable=False)
    pair_group: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weight_override: Mapped[float | None] = mapped_column(Numeric(18, 9))
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    project = relationship("Project", back_populates="observations")
    from_point = relationship("Point", foreign_keys=[from_point_id])
    to_point = relationship("Point", foreign_keys=[to_point_id])


class Datum(Base):
    __tablename__ = "datums"
    __table_args__ = (Index("ix_datums_project", "project_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    point_id: Mapped[int] = mapped_column(ForeignKey("points.id", ondelete="RESTRICT"), nullable=False)
    elevation_m: Mapped[float] = mapped_column(Numeric(18, 9), nullable=False)
    sigma_m: Mapped[float] = mapped_column(Numeric(15, 9), default=0.001, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    point = relationship("Point")
    project = relationship("Project", back_populates="datums")


class WeightRule(Base):
    __tablename__ = "weight_rules"
    __table_args__ = (Index("ix_weight_rules_project", "project_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Example: {"method":"distance_inverse_km","c_km":1.0,"base_sigma_m":0.001}
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    project = relationship("Project", back_populates="weight_rules")


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshot_project_version", "project_id", "version", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=SnapshotKind.OBSERVATIONS_RULES, nullable=False)
    observations_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    algorithm: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("generation_key", name="uq_job_generation"),
        Index("ix_jobs_project_snapshot", "project_id", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="RESTRICT"), nullable=False)
    generation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING, nullable=False)
    current_stage: Mapped[str] = mapped_column(String(64), default="import_qc", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    diagnostics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    snapshot = relationship("Snapshot")
    stages = relationship("JobStage", back_populates="job", cascade="all, delete-orphan")


class JobStage(Base):
    __tablename__ = "job_stages"
    __table_args__ = (UniqueConstraint("job_id", "name", name="uq_stage_job_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=StageStatus.PENDING, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job = relationship("Job", back_populates="stages")


class ComponentResult(Base):
    __tablename__ = "component_results"
    __table_args__ = (Index("ix_component_job", "job_id", "component_index", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    component_index: Mapped[int] = mapped_column(Integer, nullable=False)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    datum_count: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    degrees_of_freedom: Mapped[int | None] = mapped_column(Integer)
    condition_number: Mapped[float | None] = mapped_column(Numeric(24, 8))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    elevations: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    diagnostics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ObservationResult(Base):
    __tablename__ = "observation_results"
    __table_args__ = (
        UniqueConstraint("job_id", "line_code", name="uq_result_job_line"),
        Index("ix_obs_result_job", "job_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("observations.id", ondelete="SET NULL"))
    line_code: Mapped[str] = mapped_column(String(80), nullable=False)
    observed_delta_m: Mapped[float] = mapped_column(Numeric(18, 9), nullable=False)
    adjusted_delta_m: Mapped[float | None] = mapped_column(Numeric(18, 9))
    correction_m: Mapped[float | None] = mapped_column(Numeric(18, 9))
    residual_v: Mapped[float | None] = mapped_column(Numeric(18, 9))
    weight: Mapped[float | None] = mapped_column(Numeric(20, 10))


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_publication_project_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checks: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    algorithm: Mapped[dict] = mapped_column(JSONB, nullable=False)
    elevations: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("lock_version_out = lock_version_in + 1", name="ck_optimistic_version_sequence"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version_in: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version_out: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
