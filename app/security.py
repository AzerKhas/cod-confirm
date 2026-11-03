"""Security helpers. Phase 0: masking of personal data / secrets for logs.

Signature verification, password hashing and encryption are added in later phases.
"""

from __future__ import annotations


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
