"""Encryption at rest for shop secrets (WhatsApp access tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key comes from ENCRYPTION_KEY:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

In dev/test, when ENCRYPTION_KEY is empty, a key is derived from SECRET_KEY so the app
works out of the box. Production refuses to start without ENCRYPTION_KEY (see config).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings, get_settings


class SecretDecryptError(RuntimeError):
    """Stored secret cannot be decrypted (wrong ENCRYPTION_KEY or corrupted value)."""


def _fernet(settings: Settings | None = None) -> Fernet:
    s = settings or get_settings()
    key = s.encryption_key.get_secret_value()
    if not key:
        if s.app_env == "prod":
            raise RuntimeError("ENCRYPTION_KEY is required in production")
        digest = hashlib.sha256(b"cod-confirm-dev:" + s.secret_key.get_secret_value().encode())
        key = base64.urlsafe_b64encode(digest.digest()).decode()
    return Fernet(key.encode())


def encrypt_secret(plain: str, settings: Settings | None = None) -> str:
    return _fernet(settings).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, settings: Settings | None = None) -> str:
    try:
        return _fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptError("cannot decrypt stored secret") from exc
