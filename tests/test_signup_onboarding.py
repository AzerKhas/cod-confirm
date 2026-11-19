from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import login_throttle, verify_password
from app.config import get_settings
from app.crypto import reveal_secret
from app.enums import Language, OrderStatus, Role
from app.models import Order, Shop, User
from app.security import verify_api_key
from app.services.billing import today_for
from app.services.onboarding import slugify, unique_slug
from tests.factories import make_shop

TOKEN_RE = re.compile(r'name="ft" value="([^"]+)"')
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    login_throttle._failures.clear()
    yield
    get_settings.cache_clear()


def signup(client: TestClient, **over: str) -> Any:
    page = client.get("/signup")
    data = {"ft": TOKEN_RE.search(page.text).group(1),  # type: ignore[union-attr]
            "shop_name": "Boutique Yasmine", "email": "Yasmine@Shop.tn",
            "password": "very-long-password", "language": "fr"}  # fmt: skip
    data.update(over)
    return client.post("/signup", data=data, follow_redirects=False)


def csrf(client: TestClient, url: str = "/dashboard/onboarding") -> str:
    return CSRF_RE.search(client.get(url).text).group(1)  # type: ignore[union-attr]


# ------------------------------------------------------------------- slugs
@pytest.mark.parametrize(("name", "slug"), [("Boutique Yasmine & Co", "boutique-yasmine-co"),
                                            ("Élégance Tunis", "elegance-tunis"), ("متجر نور", "")])  # fmt: skip
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_unique_slug(db_session: Session) -> None:
    make_shop(db_session, slug="boutique-yasmine")
    assert unique_slug(db_session, "Boutique Yasmine") == "boutique-yasmine-2"
    assert unique_slug(db_session, "متجر نور") == "boutique"


# ------------------------------------------------------------------ sign-up
def test_signup_creates_trial_shop_and_logs_in(client: TestClient, db_session: Session) -> None:
    page = client.get("/signup")
    assert page.status_code == 200 and "14 jours gratuits" in page.text
    r = signup(client)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/onboarding"

    user = db_session.scalar(select(User).where(User.email == "yasmine@shop.tn"))
    assert (
        user is not None
        and user.role is Role.OWNER
        and verify_password("very-long-password", user.password_hash)
    )
    shop = db_session.get(Shop, user.shop_id)
    assert shop is not None and shop.slug == "boutique-yasmine" and shop.plan == "trial"
    assert shop.default_language is Language.FRENCH
    assert shop.paid_until == today_for(shop) + timedelta(days=14)

    onboarding = client.get("/dashboard/onboarding")
    assert onboarding.status_code == 200
    assert "Bienvenue sur COD Confirm" in onboarding.text and "0 / 4 étapes" in onboarding.text


@pytest.mark.parametrize(("over", "message"), [
    ({"email": "not-an-email"}, "Adresse email invalide"),
    ({"password": "short"}, "au moins 10"),
    ({"shop_name": "X"}, "entre 2 et 120"),
])  # fmt: skip
def test_signup_validation(
    client: TestClient, db_session: Session, over: dict[str, str], message: str
) -> None:
    r = signup(client, **over)
    assert r.status_code == 400 and message in r.text
    assert db_session.scalars(select(Shop)).all() == []
    assert "very-long-password" not in r.text  # password never echoed back


def test_signup_existing_email(client: TestClient, db_session: Session) -> None:
    signup(client)
    client.post("/logout", data={"csrf_token": csrf(client)})
    r = signup(client, shop_name="Autre")
    assert r.status_code == 400 and "existe déjà" in r.text
    assert len(db_session.scalars(select(Shop)).all()) == 1


def test_signup_protections(client: TestClient, db_session: Session) -> None:
    assert (
        client.post(
            "/signup", data={"shop_name": "A", "email": "a@b.tn", "password": "x" * 12}
        ).status_code
        == 400
    )
    assert signup(client, website="spam").headers["location"] == "/login"  # honeypot
    assert db_session.scalars(select(Shop)).all() == []
    codes = [
        signup(client, email=f"u{i}@shop.tn", shop_name=f"Shop {i}").status_code for i in range(6)
    ]
    assert codes[:5] == [303] * 5 and codes[5] == 429  # 5 sign-ups per hour per IP


def test_signup_terms_and_disable(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERMS_URL", "https://codconfirm.tn/conditions")
    get_settings.cache_clear()
    r = signup(client)
    assert r.status_code == 400 and "Acceptez les conditions" in r.text
    assert signup(client, terms="on").status_code == 303
    assert db_session.scalars(select(User)).one().terms_accepted_at is not None
    monkeypatch.setenv("SIGNUP_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/signup").status_code == 404


# --------------------------------------------------------------- onboarding
def test_connect_whatsapp_and_progress(client: TestClient, db_session: Session) -> None:
    signup(client)
    token = csrf(client)
    r = client.post("/dashboard/onboarding/whatsapp", data={"csrf_token": token, "phone_number_id": "1065403522",
                                                            "waba_id": "2000", "access_token": "EAAG-secret"},
                    follow_redirects=False)  # fmt: skip
    assert "msg=wa_saved" in r.headers["location"]
    shop = db_session.scalars(select(Shop)).one()
    assert reveal_secret(shop.access_token_encrypted) == "EAAG-secret"
    page = client.get("/dashboard/onboarding").text
    assert "EAAG-secret" not in page and "1 / 4 étapes" in page and "message test" in page

    bad = client.post("/dashboard/onboarding/whatsapp", data={"csrf_token": token, "phone_number_id": "abc"},
                      follow_redirects=False)  # fmt: skip
    assert "msg=wa_invalid" in bad.headers["location"]


@respx.mock
def test_whatsapp_test_message(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.post("https://graph.facebook.com/v21.0/111/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})
    )
    signup(client)
    token = csrf(client)
    client.post("/dashboard/onboarding/whatsapp", data={"csrf_token": token, "phone_number_id": "111",
                                                        "access_token": "tok"})  # fmt: skip
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    r = client.post("/dashboard/onboarding/whatsapp/test", data={"csrf_token": token, "phone": "98 123 456"},
                    follow_redirects=False)  # fmt: skip
    assert "msg=wa_test_ok" in r.headers["location"]
    body = route.calls.last.request.read().decode()
    assert '"hello_world"' in body and '"21698123456"' in body
    assert "msg=phone_invalid" in client.post("/dashboard/onboarding/whatsapp/test",
        data={"csrf_token": token, "phone": "71 000 000"}, follow_redirects=False).headers["location"]  # fmt: skip


def test_test_order_reaches_owner(client: TestClient, db_session: Session) -> None:
    signup(client)
    r = client.post("/dashboard/onboarding/test-order", data={"csrf_token": csrf(client), "phone": "98 765 432"},
                    follow_redirects=False)  # fmt: skip
    assert "msg=test_ok" in r.headers["location"]  # DRY_RUN sends
    order = db_session.scalars(select(Order)).one()
    assert (
        order.customer.phone_e164 == "+21698765432"
        and order.status is OrderStatus.AWAITING_CONFIRMATION
    )
    assert "2 / 4 étapes" in client.get("/dashboard/onboarding").text  # orders + test done


def test_integrations_and_api_key(client: TestClient, db_session: Session) -> None:
    signup(client)
    token = csrf(client)
    client.post("/dashboard/integrations", data={"csrf_token": token, "shopify_secret": "shpss_1"})
    shop = db_session.scalars(select(Shop)).one()
    db_session.expire_all()
    assert reveal_secret(shop.shopify_webhook_secret) == "shpss_1"

    r = client.post("/dashboard/api-key", data={"csrf_token": token})
    key = re.search(r'data-secret="api_key">([^<]+)<', r.text).group(1)  # type: ignore[union-attr]
    db_session.expire_all()
    assert verify_api_key(key, shop.api_key_hash)
    order = {"customer_name": "A", "phone": "98123456", "address": "R", "city": "Tunis",
             "items": [{"name": "X", "qty": 1, "price": "10"}]}  # fmt: skip
    assert client.post("/api/orders", json=order, headers={"X-API-Key": key}).status_code == 201


def test_onboarding_is_owner_only_and_csrf(client: TestClient, db_session: Session) -> None:
    from app.auth import hash_password

    shop = make_shop(db_session)
    db_session.add(User(email="staff@s.tn", password_hash=hash_password("staff-password-1"), role=Role.STAFF,
                        shop_id=shop.id))  # fmt: skip
    db_session.commit()
    client.post("/login", data={"email": "staff@s.tn", "password": "staff-password-1"})
    assert client.get("/dashboard/onboarding").status_code == 403
    signup(client, email="o@s.tn", shop_name="Owner shop")
    assert client.post("/dashboard/api-key").status_code == 403  # no CSRF token


def test_dashboard_shows_setup_banner(client: TestClient) -> None:
    signup(client)
    page = client.get("/dashboard").text
    assert "Terminez la configuration (0/4)" in page and 'href="/dashboard/onboarding"' in page
    assert "/signup" in client.get("/login").text
