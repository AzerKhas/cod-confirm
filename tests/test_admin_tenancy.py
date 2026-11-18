from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle
from app.crypto import decrypt_secret
from app.enums import OrderStatus, Role
from app.models import Order, Shop, UsageCounter, User
from app.plans import Plan, hard_quota
from app.services import usage
from app.services.notify import send_order_confirmation
from tests.factories import make_order, make_shop, make_shop_with_key

PASSWORD = "admin-password-very-long"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ORDER = {"customer_name": "Ahmed", "phone": "98123456", "address": "Rue 1", "city": "Tunis",
         "items": [{"name": "Robe", "qty": 1, "price": "45"}]}  # fmt: skip


@pytest.fixture(autouse=True)
def _reset() -> None:
    login_throttle._failures.clear()


def login_as(
    client: TestClient, db: Session, role: Role = Role.SUPERADMIN, shop: Shop | None = None
) -> str:
    email = f"{role.value}@codconfirm.tn"
    db.add(User(email=email, password_hash=hash_password(PASSWORD), role=role,
                shop_id=shop.id if shop else None))  # fmt: skip
    db.commit()
    client.post("/login", data={"email": email, "password": PASSWORD})
    page = client.get("/admin" if role is Role.SUPERADMIN else "/dashboard")
    match = CSRF_RE.search(page.text)
    assert match
    return match.group(1)


# ---------------------------------------------------------------- access control
def test_admin_requires_superadmin(client: TestClient, db_session: Session) -> None:
    assert client.get("/admin", follow_redirects=False).status_code == 303  # not logged in
    shop = make_shop(db_session)
    login_as(client, db_session, Role.OWNER, shop)
    assert client.get("/admin").status_code == 403
    assert client.get(f"/admin/shops/{shop.id}").status_code == 403


def test_superadmin_login_redirects_to_admin(client: TestClient, db_session: Session) -> None:
    db_session.add(
        User(email="ops@codconfirm.tn", password_hash=hash_password(PASSWORD), role=Role.SUPERADMIN)
    )
    db_session.commit()
    r = client.post(
        "/login", data={"email": "ops@codconfirm.tn", "password": PASSWORD}, follow_redirects=False
    )
    assert r.headers["location"] == "/admin"
    assert client.get("/dashboard").status_code == 403  # no shop attached


# --------------------------------------------------------------- shop creation
def test_create_shop_shows_secrets_once(client: TestClient, db_session: Session) -> None:
    token = login_as(client, db_session)
    r = client.post("/admin/shops/new", data={
        "csrf_token": token, "name": "Cosmétiques Nour", "slug": "cosmetiques-nour", "plan": "starter",
        "default_language": "ar", "owner_email": "Nour@Shop.tn"})  # fmt: skip
    assert r.status_code == 200
    api_key = re.search(r"(cc_[A-Za-z0-9_-]+)", r.text)
    password = re.search(r'data-secret="password">([^<]+)<', r.text)
    assert api_key and password

    shop = db_session.scalar(select(Shop).where(Shop.slug == "cosmetiques-nour"))
    assert shop is not None and shop.plan == "starter"
    assert api_key.group(1) not in shop.api_key_hash  # only the hash is stored
    owner = db_session.scalar(select(User).where(User.email == "nour@shop.tn"))
    assert owner is not None and owner.role is Role.OWNER and owner.shop_id == shop.id

    # The key works for the intake API, the password for the dashboard.
    assert (
        client.post("/api/orders", json=ORDER, headers={"X-API-Key": api_key.group(1)}).status_code
        == 201
    )
    assert (
        client.get(f"/admin/shops/{shop.id}").text.count(api_key.group(1)) == 0
    )  # never shown again


@pytest.mark.parametrize(("slug", "email"), [("Bad Slug!", "a@b.tn"), ("ok-slug", "not-an-email"),
                                              ("demo", "x@y.tn")])  # fmt: skip
def test_create_shop_validation(
    client: TestClient, db_session: Session, slug: str, email: str
) -> None:
    make_shop(db_session, slug="demo")
    token = login_as(client, db_session)
    r = client.post("/admin/shops/new", data={"csrf_token": token, "name": "X", "slug": slug,
                                              "plan": "pro", "default_language": "fr", "owner_email": email})  # fmt: skip
    assert r.status_code == 400


def test_create_shop_requires_csrf(client: TestClient, db_session: Session) -> None:
    login_as(client, db_session)
    r = client.post("/admin/shops/new", data={"name": "X", "slug": "xx-shop", "plan": "pro",
                                              "default_language": "fr", "owner_email": "a@b.tn"})  # fmt: skip
    assert r.status_code == 403


# ------------------------------------------------------------- WhatsApp settings
def test_whatsapp_token_stored_encrypted(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    token = login_as(client, db_session)
    client.post(f"/admin/shops/{shop.id}/whatsapp", data={
        "csrf_token": token, "phone_number_id": "1065403522", "waba_id": "2000",
        "access_token": "EAAG-real-token", "shopify_secret": "shpss_x", "woocommerce_secret": ""})  # fmt: skip
    db_session.expire_all()
    assert shop.whatsapp_phone_number_id == "1065403522"
    assert shop.access_token_encrypted and "EAAG" not in shop.access_token_encrypted
    assert decrypt_secret(shop.access_token_encrypted) == "EAAG-real-token"
    page = client.get(f"/admin/shops/{shop.id}").text
    assert "EAAG-real-token" not in page and "shpss_x" not in page

    # Empty fields keep the stored secrets.
    client.post(f"/admin/shops/{shop.id}/whatsapp", data={"csrf_token": token, "phone_number_id": "1065403522",
                                                          "waba_id": "2000", "access_token": ""})  # fmt: skip
    db_session.expire_all()
    assert decrypt_secret(shop.access_token_encrypted or "") == "EAAG-real-token"


def test_regenerate_api_key_invalidates_old(client: TestClient, db_session: Session) -> None:
    shop, old_key = make_shop_with_key(db_session)
    token = login_as(client, db_session)
    r = client.post(f"/admin/shops/{shop.id}/api-key", data={"csrf_token": token})
    new_key = re.search(r"(cc_[A-Za-z0-9_-]+)", r.text)
    assert new_key
    assert client.post("/api/orders", json=ORDER, headers={"X-API-Key": old_key}).status_code == 401
    assert (
        client.post("/api/orders", json=ORDER, headers={"X-API-Key": new_key.group(1)}).status_code
        == 201
    )


def test_reset_password_logs_user_out(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    owner = User(
        email="o@shop.tn", password_hash=hash_password(PASSWORD), role=Role.OWNER, shop_id=shop.id
    )
    db_session.add(owner)
    db_session.commit()
    token = login_as(client, db_session)
    r = client.post(f"/admin/users/{owner.id}/reset", data={"csrf_token": token})
    assert r.status_code == 200
    db_session.expire_all()
    assert owner.session_version == 2


# ---------------------------------------------------------- suspension / quotas
def test_suspended_shop(client: TestClient, db_session: Session) -> None:
    shop, key = make_shop_with_key(db_session, slug="suspended", shopify_webhook_secret="s")
    token = login_as(client, db_session)
    client.post(f"/admin/shops/{shop.id}/subscription", data={"csrf_token": token, "plan": "starter",
                                                              "paid_until": "2026-12-01"})  # fmt: skip
    db_session.expire_all()
    assert shop.is_active is False and shop.plan == "starter"

    assert client.post("/api/orders", json=ORDER, headers={"X-API-Key": key}).status_code == 403
    assert client.get("/form/suspended").status_code == 404
    order = make_order(db_session, shop)
    assert send_order_confirmation(db_session, order) is None  # nothing sent
    assert order.status is OrderStatus.NEW


def test_quota_exceeded_saves_orders_but_sends_nothing(db_session: Session) -> None:
    shop = make_shop(db_session, plan=Plan.TRIAL.value)
    counter = usage.counter(db_session, shop.id)
    counter.orders = hard_quota(Plan.TRIAL) + 1
    order = make_order(db_session, shop)
    assert send_order_confirmation(db_session, order) is None
    assert order.status is OrderStatus.NEW
    assert usage.can_send(db_session, shop) == (False, "monthly quota exceeded")


def test_usage_counters(client: TestClient, db_session: Session) -> None:
    shop, key = make_shop_with_key(db_session)
    db_session.commit()
    for i in range(3):
        client.post(
            "/api/orders", json={**ORDER, "external_id": str(i)}, headers={"X-API-Key": key}
        )
    client.post(
        "/api/orders", json={**ORDER, "external_id": "0"}, headers={"X-API-Key": key}
    )  # duplicate
    db_session.expire_all()
    row = db_session.scalar(select(UsageCounter).where(UsageCounter.shop_id == shop.id))
    assert row is not None
    assert row.month == usage.month_key()
    assert row.orders == 3  # duplicates are not counted
    assert row.messages_sent == 3  # one confirmation each (DRY_RUN)


def test_admin_list_shows_usage(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, name="Sbabet Ali", plan="pro")
    usage.bump(db_session, shop.id, orders=42, messages=80)
    db_session.commit()
    login_as(client, db_session)
    page = client.get("/admin").text
    assert "Sbabet Ali" in page and "42 / 1500" in page
    assert "129 DT" in page  # MRR


# ---------------------------------------------------------------- rate limits
def test_api_rate_limit_per_shop(client: TestClient, db_session: Session) -> None:
    _shop, key = make_shop_with_key(db_session, plan="trial")  # 30 requests / minute
    db_session.commit()
    codes = [client.post("/api/orders", json={**ORDER, "external_id": f"r{i}"},
                         headers={"X-API-Key": key}).status_code for i in range(31)]  # fmt: skip
    assert codes[:30].count(201) == 30
    assert codes[30] == 429


def test_form_rate_limit_per_ip(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="busy")
    db_session.commit()
    codes = [client.post("/form/busy", data={"website": "bot"}, follow_redirects=False).status_code
             for _ in range(11)]  # fmt: skip
    assert codes[:10] == [400] * 10  # no form token, but each one counts for the limit
    assert codes[10] == 429


def test_tenant_data_never_leaks_between_shops(client: TestClient, db_session: Session) -> None:
    a, key_a = make_shop_with_key(db_session)
    b, key_b = make_shop_with_key(db_session)
    db_session.commit()
    ra = client.post(
        "/api/orders", json={**ORDER, "customer_name": "Client A"}, headers={"X-API-Key": key_a}
    )
    rb = client.post(
        "/api/orders", json={**ORDER, "customer_name": "Client B"}, headers={"X-API-Key": key_b}
    )
    oa, ob = db_session.get(Order, ra.json()["id"]), db_session.get(Order, rb.json()["id"])
    assert oa and ob and oa.customer_id != ob.customer_id  # same phone, two separate customers
    assert oa.shop_id == a.id and ob.shop_id == b.id
