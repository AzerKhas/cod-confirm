"""Security helpers: intake API keys (hashed), masking of personal data / secrets for logs.

Signature verification, password hashing and encryption are added in later phases.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

API_KEY_PREFIX = "cc_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (plain_key, sha256_hash, display_prefix). Show plain_key once, store the hash."""
    plain = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_api_key(plain), plain[:10]


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_api_key(plain: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plain), stored_hash)


def mask_phone(phone: str | None) -> str:
    """Mask a phone number for logs: '+21698123456' -> '+216*****456'."""
    if not phone:
        return ""
    digits_kept = 3
    prefix = phone[:4] if phone.startswith("+") else phone[:3]
    if len(phone) <= len(prefix) + digits_kept:
        return "*" * len(phone)
    hidden = len(phone) - len(prefix) - digits_kept
    return f"{prefix}{'*' * hidden}{phone[-digits_kept:]}"


def mask_secret(secret: str | None) -> str:
    """Mask a token/secret for logs: keep only the last 4 chars."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"****{secret[-4:]}"
