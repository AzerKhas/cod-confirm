"""Phase 9: security hardening checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.crypto import decrypt_secret, encrypt_secret, reveal_secret, rotate
from app.security import hmac_sha256_base64
from tests.factories import make_shop

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------- headers
@pytest.mark.parametrize("path", ["/health", "/login", "/form/nope"])
def test_security_headers(client: TestClient, path: str) -> None:
    h = client.get(path).headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = h["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "'unsafe-inline'" not in csp  # no inline scripts nor inline styles
    assert "tailwindcss" not in csp  # design system is self-hosted


def test_private_pages_not_cached(client: TestClient) -> None:
    assert client.get("/login").headers["cache-control"] == "no-store"


def test_no_inline_javascript_in_templates() -> None:
    inline_handler = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
    inline_script = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
    for tpl in TEMPLATES.rglob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert not inline_handler.search(text), f"inline event handler in {tpl.name}"
        assert not inline_script.search(text), f"inline <script> in {tpl.name}"


def test_no_inline_styles_in_templates() -> None:
    for tpl in TEMPLATES.rglob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", text), f"inline style attribute in {tpl.name}"
        assert "<style" not in text, f"<style> block in {tpl.name}"


def test_static_js_served(client: TestClient) -> None:
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "data-confirm" in r.text


def test_session_cookie_flags(client: TestClient, db_session: Session) -> None:
    from app.auth import hash_password
    from app.enums import Role
    from app.models import User

    db_session.add(
        User(email="a@b.tn", password_hash=hash_password("long-password-1"), role=Role.SUPERADMIN)
    )
    db_session.commit()
    r = client.post(
        "/login", data={"email": "a@b.tn", "password": "long-password-1"}, follow_redirects=False
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


# --------------------------------------------------------------- body limits
def test_global_body_limit(client: TestClient) -> None:
    r = client.post(
        "/login",
        content=b"x" * 1_000_001,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


def test_body_limit_without_content_length(client: TestClient) -> None:
    def chunks():  # type: ignore[no-untyped-def]
        for _ in range(20):
            yield b"x" * 100_000

    r = client.post("/webhook/meta", content=chunks())
    assert r.status_code == 413


# ------------------------------------------------------------- trusted hosts
def test_unknown_host_rejected(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    from app.main import create_app

    monkeypatch.setenv("ALLOWED_HOSTS", "app.codconfirm.tn")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app, base_url="http://evil.example") as c:
            assert c.get("/health").status_code == 400
        with TestClient(app, base_url="http://app.codconfirm.tn") as c:
            assert c.get("/static/app.js").status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("hosts", ["", "*"])
def test_prod_requires_allowed_hosts(hosts: str) -> None:
    with pytest.raises(ValueError):
        _settings(app_env="prod", secret_key="x" * 40, encryption_key=Fernet.generate_key().decode(),
                  allowed_hosts=hosts)  # fmt: skip


# ---------------------------------------------------------- secrets at rest
def test_key_rotation() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    s_old = _settings(encryption_key=old)
    s_both = _settings(encryption_key=f"{new},{old}")
    token = encrypt_secret("EAAG-token", s_old)
    assert decrypt_secret(token, s_both) == "EAAG-token"  # old data readable during rotation
    rotated = rotate(token, s_both)
    assert rotated and decrypt_secret(rotated, _settings(encryption_key=new)) == "EAAG-token"


def test_legacy_plain_secret_still_works_and_is_encrypted_by_rotation() -> None:
    s = _settings(encryption_key=Fernet.generate_key().decode())
    assert reveal_secret("plain-old-secret", s) == "plain-old-secret"
    rotated = rotate("plain-old-secret", s)
    assert (
        rotated and rotated.startswith("gAAAAA") and reveal_secret(rotated, s) == "plain-old-secret"
    )
    assert reveal_secret(None, s) is None


def test_encrypted_shopify_secret_verifies_webhook(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="enc", shopify_webhook_secret=encrypt_secret("shpss_live"))
    db_session.commit()
    body = b'{"id": 1}'
    r = client.post("/webhooks/shopify/enc", content=body, headers={
        "X-Shopify-Hmac-Sha256": hmac_sha256_base64("shpss_live", body), "X-Shopify-Topic": "orders/updated"})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "ignored"  # signature OK, topic ignored


def test_rotate_secrets_script(db_session: Session) -> None:
    from scripts.rotate_secrets import main

    shop = make_shop(
        db_session,
        woocommerce_webhook_secret="legacy-plain",
        access_token_encrypted=encrypt_secret("tok"),
    )
    db_session.commit()
    main()
    db_session.expire_all()
    assert shop.woocommerce_webhook_secret and shop.woocommerce_webhook_secret.startswith("gAAAAA")
    assert reveal_secret(shop.woocommerce_webhook_secret) == "legacy-plain"
    assert reveal_secret(shop.access_token_encrypted) == "tok"


# ------------------------------------------------------------ misc hardening
def test_api_docs_hidden_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    for k, v in {"APP_ENV": "prod", "SECRET_KEY": "x" * 40, "ENCRYPTION_KEY": Fernet.generate_key().decode(),
                 "ALLOWED_HOSTS": "testserver"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            assert c.get("/docs").status_code == 404
            assert c.get("/openapi.json").status_code == 404
            assert "strict-transport-security" in c.get("/static/app.js").headers
    finally:
        get_settings.cache_clear()


def test_error_responses_do_not_leak_internals(client: TestClient) -> None:
    r = client.post(
        "/api/orders",
        content=b"{not json",
        headers={"X-API-Key": "cc_x", "Content-Type": "application/json"},
    )
    assert "Traceback" not in r.text and "sqlalchemy" not in r.text.lower()
