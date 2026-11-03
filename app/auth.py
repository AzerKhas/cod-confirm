"""Dashboard authentication: password hashing, sessions, CSRF, login throttling.

* Passwords: scrypt (stdlib, memory-hard), random 16-byte salt, constant-time compare.
* Sessions: signed cookie (Starlette SessionMiddleware, itsdangerous) holding the user id
  and `session_version`; bumping the version logs the user out everywhere.
* CSRF: per-session token, required on every POST (form field or X-CSRF-Token header).
* Login throttling: 5 failures per 15 minutes per (IP, email).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from threading import Lock

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import Role
from app.models import Shop, User

# ------------------------------------------------------------------ passwords
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# A valid hash used when the email is unknown, so timing doesn't reveal which emails exist.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def authenticate(db: Session, email: str, password: str) -> User | None:
    from sqlalchemy import select

    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


# -------------------------------------------------------------- login throttle
class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_seconds: int = 900) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _key(self, ip: str, email: str) -> str:
        return f"{ip}|{email.strip().lower()}"

    def blocked(self, ip: str, email: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._failures[self._key(ip, email)] if now - t < self.window]
            self._failures[self._key(ip, email)] = hits
            return len(hits) >= self.max_failures

    def fail(self, ip: str, email: str) -> None:
        with self._lock:
            self._failures[self._key(ip, email)].append(time.monotonic())

    def reset(self, ip: str, email: str) -> None:
        with self._lock:
            self._failures.pop(self._key(ip, email), None)


login_throttle = LoginThrottle()


# -------------------------------------------------------------------- session
def login_session(request: Request, user: User) -> None:
    request.session.clear()  # new session on login (prevents session fixation)
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version
    request.session["csrf"] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


class LoginRequired(Exception):
    """Raised by `current_user`; turned into a redirect to /login."""


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not isinstance(uid, int):
        raise LoginRequired
    user = db.get(User, uid)
    if user is None or not user.is_active or request.session.get("sv") != user.session_version:
        request.session.clear()
        raise LoginRequired
    return user


def current_shop_user(user: User = Depends(current_user)) -> User:
    """A user attached to a shop (owner or staff)."""
    if user.shop_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no shop attached to this account")
    return user


def require_owner(user: User = Depends(current_shop_user)) -> User:
    if user.role is not Role.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner only")
    return user


def require_superadmin(user: User = Depends(current_user)) -> User:
    if user.role is not Role.SUPERADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "superadmin only")
    return user


def user_shop(db: Session, user: User) -> Shop:
    shop = db.get(Shop, user.shop_id) if user.shop_id else None
    if shop is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return shop


# ----------------------------------------------------------------------- CSRF
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


async def csrf_protect(request: Request) -> None:
    """Dependency for every state-changing route of the dashboard / admin."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not hmac.compare_digest(str(expected), sent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
