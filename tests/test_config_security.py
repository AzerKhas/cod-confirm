from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging_config import JsonFormatter
from app.security import mask_phone, mask_secret


def test_prod_refuses_placeholder_secret_key() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="prod", secret_key="change-me", _env_file=None)  # type: ignore[arg-type]


def test_prod_requires_meta_secret_when_not_dry_run() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="prod", secret_key="x" * 40, dry_run=False, _env_file=None  # type: ignore[arg-type]
        )


def test_prod_ok_with_strong_secret() -> None:
    s = Settings(app_env="prod", secret_key="x" * 40, _env_file=None)  # type: ignore[arg-type]
    assert s.dry_run is True


def test_secrets_not_in_repr() -> None:
    s = Settings(meta_app_secret="super-secret-value", _env_file=None)  # type: ignore[arg-type]
    assert "super-secret-value" not in repr(s)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+21698123456", "+216*****456"),
        ("98123456", "981**456"),
        ("", ""),
        (None, ""),
        ("123", "***"),
    ],
)
def test_mask_phone(raw: str | None, expected: str) -> None:
    assert mask_phone(raw) == expected


def test_mask_secret() -> None:
    assert mask_secret("EAAGabcdefgh1234") == "****1234"
    assert mask_secret("short") == "****"
    assert mask_secret(None) == ""


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "hello", "order_id": 42}
    )
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello"
    assert out["order_id"] == 42
    assert out["level"] == "INFO"
