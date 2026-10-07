"""Test harness: file-backed SQLite + synchronous pipeline, no external services.

The app is imported with LEVEL_DATABASE_URL pointing at a temporary SQLite
*file* so API sessions and worker sessions share one database, and a process
restart can be simulated by disposing every connection and reopening new ones
against the same file. JSONB/Geometry get SQLite compiler shims, and the
geoalchemy2 SpatiaLite DDL hooks are neutralized (geometry is unused in these
tests).
"""
from __future__ import annotations

import os
import tempfile

_TEST_DIR = tempfile.mkdtemp(prefix="leveling_test_")
os.environ["LEVEL_DATABASE_URL"] = f"sqlite:///{_TEST_DIR}/test.db"
os.environ.setdefault("LEVEL_REDIS_URL", "redis://localhost:6379/0")

from geoalchemy2 import Geometry  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202, D103
    return "JSON"


@compiles(Geometry, "sqlite")
def _geometry_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202, D103
    return "BLOB"


import geoalchemy2.admin.dialects.sqlite as _ga_sqlite  # noqa: E402

_ga_sqlite.before_create = lambda table, bind, **kw: None
_ga_sqlite.after_create = lambda table, bind, **kw: None
_ga_sqlite.before_drop = lambda table, bind, **kw: None
_ga_sqlite.after_drop = lambda table, bind, **kw: None

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import schema  # noqa: E402, F401  (register models on metadata)
from app.workers import tasks  # noqa: E402


@event.listens_for(engine, "connect")
def _register_spatial_stubs(dbapi_conn, _record):  # noqa: ANN001, ANN202
    # geoalchemy2 wraps geometry columns in SpatiaLite functions; geometry is
    # never populated in these tests, so pass-through stubs are enough.
    dbapi_conn.create_function("AsEWKB", 1, lambda value: value)
    dbapi_conn.create_function("GeomFromEWKB", 1, lambda value: value)
    dbapi_conn.create_function("GeomFromEWKT", 1, lambda value: value)


class _FakeRedis:
    """Minimal lock client stand-in for the pipeline generation lock."""

    def __init__(self) -> None:
        self.held: str | None = None

    def set(self, name, value, nx=False, ex=None):  # noqa: ANN001, ANN201
        if nx and self.held == name:
            return None
        self.held = name
        return True

    def delete(self, *names):  # noqa: ANN001, ANN201
        self.held = None


class _EagerPipeline:
    """Runs the celery pipeline synchronously, like a worker would."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id

    def apply_async(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        return tasks.run_pipeline.apply(args=[self.job_id])


@pytest.fixture(autouse=True)
def _database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda url: client)
    return client


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    # No lifespan: the startup hook issues PostGIS-specific DDL.
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def eager_pipeline(monkeypatch):
    monkeypatch.setattr("app.api.routes.build_pipeline", lambda job_id: _EagerPipeline(job_id))
