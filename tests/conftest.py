"""Shared fixtures: isolated settings + in-memory SQLite DB per test."""

from __future__ import annotations

import os
from collections.abc import Iterator

# Must be set before `app` is imported anywhere.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DRY_RUN", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db, make_engine


@pytest.fixture
def db_session() -> Iterator[Session]:
    # StaticPool = one shared connection, so the in-memory DB survives across sessions.
    # make_engine also turns on SQLite foreign keys (needed for ON DELETE CASCADE).
    engine = make_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c
