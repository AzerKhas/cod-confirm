from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import Settings
from app.monitoring import OperatorAlertHandler

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///./x.db", "sqlite:///./x.db"),
    ],
)
def test_database_url_normalized(raw: str, expected: str) -> None:
    assert Settings(database_url=raw, _env_file=None).database_url == expected  # type: ignore[arg-type]


def test_operator_alerts_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    handler = OperatorAlertHandler("TOKEN", "42", min_interval=60)
    monkeypatch.setattr(handler, "_send", sent.append)
    monkeypatch.setattr(
        "threading.Thread.start", lambda self: self._target(*self._args)
    )  # run inline
    logger = logging.getLogger("app.test_alerts")
    logger.addHandler(handler)
    try:
        for i in range(5):
            logger.error("boom %s", i)
        logger.warning("not an error")
    finally:
        logger.removeHandler(handler)
    assert len(sent) == 1 and "boom 0" in sent[0]
    handler._last = 0  # a minute later
    logger.addHandler(handler)
    try:
        logger.error("again")
    finally:
        logger.removeHandler(handler)
    assert "+4 other error(s)" in sent[1]


def test_prod_compose_keeps_database_private() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    db_block = compose.split("  db:")[1].split("\n  app:")[0]
    assert "ports:" not in db_block
    assert "internal: true" in compose


def test_dockerfile_runs_as_non_root_with_lockfile() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER appuser" in dockerfile
    assert "requirements.lock" in dockerfile
