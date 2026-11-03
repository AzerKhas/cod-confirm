"""Security helpers: intake API keys (hashed), masking of personal data / secrets for logs.

Signature verification, password hashing and encryption are added in later phases.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

API_KEY_PREFIX = "cc_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (plain_key, sha256_hash, display_prefix). Show plain_key once, store the hash."""
    plain = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_api_key(plain), plain[:10]


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_api_key(plain: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plain), stored_hash)


FORM_TOKEN_MAX_AGE = 2 * 3600


def _form_serializer() -> Any:
    from itsdangerous import URLSafeTimedSerializer

    from app.config import get_settings

    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt="order-form")


def make_form_token(shop_slug: str) -> str:
    """Signed token embedded in the public order form (valid FORM_TOKEN_MAX_AGE seconds)."""
    return str(_form_serializer().dumps(shop_slug))


def check_form_token(token: str | None, shop_slug: str) -> bool:
    from itsdangerous import BadSignature

    if not token or len(token) > 300:
        return False
    try:
        return bool(_form_serializer().loads(token, max_age=FORM_TOKEN_MAX_AGE) == shop_slug)
    except BadSignature:  # includes SignatureExpired
        return False


def hmac_sha256_base64(secret: str, body: bytes) -> str:
    """base64(HMAC-SHA256(secret, body)) - the scheme used by Shopify and WooCommerce."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_base64_hmac(secret: str | None, body: bytes, signature: str | None) -> bool:
    """Constant-time check of a base64 HMAC-SHA256 signature header."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(hmac_sha256_base64(secret, body), signature.strip())


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
