from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle
from app.enums import Role
from app.models import Shop, User
from app.services.branding import (
    BrandingError,
    contrast,
    normalize_hex,
    palette,
    theme_css,
    validate_logo,
)
from tests.factories import make_shop

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(autouse=True)
def _reset() -> None:
    login_throttle._failures.clear()


def owner(client: TestClient, db: Session, shop: Shop) -> str:
    db.add(
        User(
            email="o@b.tn",
            password_hash=hash_password("owner-password-1"),
            role=Role.OWNER,
            shop_id=shop.id,
        )
    )
    db.commit()
    client.post("/login", data={"email": "o@b.tn", "password": "owner-password-1"})
    return CSRF_RE.search(client.get("/dashboard/settings").text).group(1)  # type: ignore[union-attr]


# ------------------------------------------------------------------ palette
@pytest.mark.parametrize("raw", ["#1660B0", "1660b0", " #1660b0 "])
def test_normalize_hex(raw: str) -> None:
    assert normalize_hex(raw) == "#1660b0"


@pytest.mark.parametrize("raw", ["red", "#fff", "#12345g", "", "url(x)", "#1660b0;}body{"])
def test_invalid_colors(raw: str) -> None:
    with pytest.raises(BrandingError):
        normalize_hex(raw)


@pytest.mark.parametrize(
    "brand", ["#1660b0", "#ffd60a", "#e63946", "#000000", "#ffffff", "#2a9d8f", "#f4a3c0"]
)
def test_palette_is_readable(brand: str) -> None:
    p = palette(brand)
    assert contrast(p["on-brand"], p["brand"]) >= 3.0  # button text on the accent
    assert contrast(p["brand-ink"], "#ffffff") >= 4.5  # accent text on white
    assert p["on-brand"] == (
        "#111111" if brand in ("#ffd60a", "#ffffff", "#f4a3c0") else p["on-brand"]
    )


def test_theme_css_covers_dark_mode() -> None:
    css = theme_css("#e63946")
    assert (
        "--brand:#e63946" in css
        and "prefers-color-scheme: dark" in css
        and 'data-theme="dark"' in css
    )


# ------------------------------------------------------------------- logos
@pytest.mark.parametrize(
    ("data", "mime"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")]
)
def test_logo_types(data: bytes, mime: str) -> None:
    assert validate_logo(data) == mime


@pytest.mark.parametrize("data", [SVG, b"", b"GIF89a" + b"\x00" * 10, PNG + b"\x00" * 300_001])
def test_logo_rejected(data: bytes) -> None:
    with pytest.raises(BrandingError):
        validate_logo(data)


# ---------------------------------------------------------------- end to end
def test_owner_sets_color_and_logo(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, slug="nour")
    token = owner(client, db_session, shop)
    assert "Apparence du formulaire" in client.get("/dashboard/settings").text

    r = client.post("/dashboard/branding", data={"csrf_token": token, "brand_color": "#E63946"},
                    files={"logo": ("logo.png", PNG, "image/png")}, follow_redirects=False)  # fmt: skip
    assert "msg=saved" in r.headers["location"]
    db_session.expire_all()
    assert shop.brand_color == "#e63946" and shop.logo_mime == "image/png"

    form = client.get("/form/nour").text
    assert 'href="/form/nour/theme.css?v=e63946"' in form and 'src="/form/nour/logo?v=' in form
    css = client.get("/form/nour/theme.css")
    assert css.headers["content-type"].startswith("text/css") and "--brand:#e63946" in css.text
    logo = client.get("/form/nour/logo")
    assert logo.content == PNG and logo.headers["content-type"] == "image/png"
    assert logo.headers["x-content-type-options"] == "nosniff"


def test_svg_disguised_as_png_is_refused(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, slug="nour")
    token = owner(client, db_session, shop)
    r = client.post("/dashboard/branding", data={"csrf_token": token, "brand_color": "#1660b0"},
                    files={"logo": ("logo.png", SVG, "image/png")}, follow_redirects=False)  # fmt: skip
    assert "msg=branding_invalid" in r.headers["location"]
    db_session.expire_all()
    assert shop.logo_mime is None


def test_reset_and_remove(client: TestClient, db_session: Session) -> None:
    shop = make_shop(
        db_session, slug="nour", brand_color="#e63946", logo=PNG, logo_mime="image/png"
    )
    token = owner(client, db_session, shop)
    client.post("/dashboard/branding", data={"csrf_token": token, "brand_color": "#e63946",
                                             "reset_color": "on", "remove_logo": "on"})  # fmt: skip
    db_session.expire_all()
    assert shop.brand_color is None and shop.logo is None
    assert client.get("/form/nour/logo").status_code == 404
    assert client.get("/form/nour/theme.css").text == ""
    assert "theme.css" not in client.get("/form/nour").text


def test_branding_requires_owner_and_csrf(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, slug="nour")
    anonymous = client.post(
        "/dashboard/branding", data={"brand_color": "#000000"}, follow_redirects=False
    )
    assert anonymous.status_code in (
        303,
        403,
    )  # blocked (CSRF check runs before the login redirect)
    db_session.add(
        User(
            email="s@b.tn",
            password_hash=hash_password("staff-password-1"),
            role=Role.STAFF,
            shop_id=shop.id,
        )
    )
    db_session.commit()
    client.post("/login", data={"email": "s@b.tn", "password": "staff-password-1"})
    assert client.post("/dashboard/branding", data={"brand_color": "#000000"}).status_code == 403
