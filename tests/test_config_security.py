from __future__ import annotations

import json
import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings
from app.crypto import SecretDecryptError, decrypt_secret, encrypt_secret
from app.logging_config import JsonFormatter
from app.security import mask_phone, mask_secret


def test_prod_refuses_placeholder_secret_key() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="prod", secret_key="change-me", _env_file=None)  # type: ignore[arg-type]


def test_prod_requires_meta_secret_when_not_dry_run() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="prod",
            secret_key="x" * 40,
            dry_run=False,
            _env_file=None,  # type: ignore[arg-type]
        )


def test_prod_ok_with_strong_secret() -> None:
    s = Settings(
        app_env="prod",
        secret_key="x" * 40,
        encryption_key=Fernet.generate_key().decode(),
        allowed_hosts="app.codconfirm.tn",
        _env_file=None,  # type: ignore[arg-type]
    )
    assert s.dry_run is True


@pytest.mark.parametrize("key", ["", "not-a-fernet-key", "YWJj"])
def test_prod_requires_valid_encryption_key(key: str) -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="prod", secret_key="x" * 40, encryption_key=key, _env_file=None)  # type: ignore[arg-type]


def test_encrypt_roundtrip_and_wrong_key() -> None:
    s1 = Settings(encryption_key=Fernet.generate_key().decode(), _env_file=None)  # type: ignore[arg-type]
    s2 = Settings(encryption_key=Fernet.generate_key().decode(), _env_file=None)  # type: ignore[arg-type]
    token = encrypt_secret("EAAG-meta-token", s1)
    assert "EAAG" not in token
    assert decrypt_secret(token, s1) == "EAAG-meta-token"
    with pytest.raises(SecretDecryptError):
        decrypt_secret(token, s2)


def test_dev_encryption_works_without_key() -> None:
    s = Settings(app_env="dev", _env_file=None)  # type: ignore[arg-type]
    assert decrypt_secret(encrypt_secret("abc", s), s) == "abc"


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
