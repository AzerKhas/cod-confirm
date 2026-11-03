from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["env"] == "test"
    assert body["dry_run"] is True


def test_health_db_down_returns_503(client: TestClient) -> None:
    class BrokenSession:
        def execute(self, *_a: object, **_k: object) -> None:
            raise OperationalError("SELECT 1", {}, Exception("db down"))

    client.app.dependency_overrides[get_db] = lambda: BrokenSession()  # type: ignore[attr-defined]
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["db"] == "error"
