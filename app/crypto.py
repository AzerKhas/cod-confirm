"""Encryption at rest for shop secrets (WhatsApp tokens, webhook signing secrets).

Fernet (AES-128-CBC + HMAC-SHA256). ENCRYPTION_KEY holds one key, or several separated by
commas for **key rotation**: the first key encrypts, every key can decrypt.

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation: put the new key first (`ENCRYPTION_KEY=new,old`), restart, run
`python -m scripts.rotate_secrets`, then remove the old key.

In dev/test, when ENCRYPTION_KEY is empty, a key is derived from SECRET_KEY so the app
works out of the box. Production refuses to start without ENCRYPTION_KEY (see config).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import Settings, get_settings

FERNET_PREFIX = "gAAAAA"  # every Fernet token starts with this (version byte 0x80)


class SecretDecryptError(RuntimeError):
    """Stored secret cannot be decrypted (wrong ENCRYPTION_KEY or corrupted value)."""


def _keys(s: Settings) -> list[str]:
    raw = s.encryption_key.get_secret_value()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if keys:
        return keys
    if s.app_env == "prod":
        raise RuntimeError("ENCRYPTION_KEY is required in production")
    digest = hashlib.sha256(b"cod-confirm-dev:" + s.secret_key.get_secret_value().encode())
    return [base64.urlsafe_b64encode(digest.digest()).decode()]


def _fernet(settings: Settings | None = None) -> MultiFernet:
    s = settings or get_settings()
    return MultiFernet([Fernet(k.encode()) for k in _keys(s)])


def encrypt_secret(plain: str, settings: Settings | None = None) -> str:
    return _fernet(settings).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, settings: Settings | None = None) -> str:
    try:
        return _fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptError("cannot decrypt stored secret") from exc


def reveal_secret(stored: str | None, settings: Settings | None = None) -> str | None:
    """Decrypt a stored secret; values saved before encryption (plain text) still work."""
    if not stored:
        return None
    if stored.startswith(FERNET_PREFIX):
        return decrypt_secret(stored, settings)
    return stored  # legacy plain value: re-encrypted by scripts/rotate_secrets.py


def rotate(stored: str | None, settings: Settings | None = None) -> str | None:
    """Re-encrypt with the primary key (also encrypts legacy plain values)."""
    plain = reveal_secret(stored, settings)
    return None if plain is None else encrypt_secret(plain, settings)
