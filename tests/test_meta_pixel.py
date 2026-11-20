from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle
from app.crypto import encrypt_secret, reveal_secret
from app.enums import OrderSource, OrderStatus, Role
from app.models import Order, Shop, User
from app.security import make_form_token
from app.services.meta_capi import (
    browser_meta,
    normalize_city,
    purchase_event,
    send_pending_purchases,
    sha256,
)
from tests.factories import make_customer, make_order, make_shop

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
EVENTS_URL = re.compile(r"https://graph\.facebook\.com/v[\d.]+/123456789012345/events")
FBP = "fb.1.1596403881668.1116446470"
FBC = "fb.1.1554763741205.AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset() -> None:
    login_throttle._failures.clear()


def pixel_shop(db: Session, **kw: object) -> Shop:
    shop = make_shop(
        db,
        slug="nour",
        meta_pixel_id="123456789012345",
        meta_capi_token=encrypt_secret("EAAG-test-token"),
        form_products=[{"name": "Robe lin", "price": "89.000"}],
        **kw,
    )
    db.commit()
    return shop


def form(**over: str) -> dict[str, str]:
    data = {"name": "Mariem", "phone": "55 444 333", "city": "Ben Arous",
            "address": "Rue 1", "product": "0", "qty": "1", "ft": make_form_token("nour")}  # fmt: skip
    data.update(over)
    return data


# ---------------------------------------------------------------- helpers
def test_hashing_and_normalization() -> None:
    assert sha256("tn") == hashlib.sha256(b"tn").hexdigest() and len(sha256("tn")) == 64
    assert normalize_city("Ben Arous") == "benarous"
    assert normalize_city("Médenine") == "medenine"


def test_browser_meta_keeps_only_valid_values() -> None:
    meta = browser_meta({"fbp": FBP, "fbc": "<script>"}, ip="41.230.1.2", user_agent="UA",
                        url="https://x.tn/form/nour")  # fmt: skip
    assert meta == {"url": "https://x.tn/form/nour", "fbp": FBP, "ip": "41.230.1.2", "ua": "UA"}
    assert "ip" not in browser_meta({}, ip="unknown", user_agent=None, url="u")


def test_purchase_event_shape(db_session: Session) -> None:
    shop = pixel_shop(db_session)
    customer = make_customer(db_session, shop, phone_e164="+21655444333")
    order = make_order(db_session, shop, customer, OrderStatus.CONFIRMED, city="Ben Arous",
                       ad_consent=True, ad_meta={"fbp": FBP, "fbc": FBC, "ip": "41.230.1.2",
                                                 "ua": "Mozilla", "url": "https://x.tn/form/nour"})  # fmt: skip
    event = purchase_event(order, NOW)
    assert event["event_name"] == "Purchase" and event["action_source"] == "website"
    assert event["event_id"] == f"purchase-{order.id}"
    assert event["event_time"] == int(NOW.timestamp())
    assert event["event_source_url"] == "https://x.tn/form/nour"
    ud = event["user_data"]
    assert ud["ph"] == [sha256("21655444333")] and ud["country"] == [sha256("tn")]
    assert ud["ct"] == [sha256("benarous")]
    assert ud["fbp"] == FBP and ud["fbc"] == FBC
    assert ud["client_ip_address"] == "41.230.1.2" and ud["client_user_agent"] == "Mozilla"
    cd = event["custom_data"]
    assert cd["currency"] == "TND" and cd["value"] == 59.0 and cd["order_id"] == str(order.id)
    assert cd["contents"][0]["quantity"] == 1


# ------------------------------------------------------------------- form
def test_form_without_pixel_has_no_meta(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="plain")
    db_session.commit()
    r = client.get("/form/plain")
    assert "pixel.js" not in r.text and "ads-consent" not in r.text
    assert "connect.facebook.net" not in r.headers["content-security-policy"]


def test_form_with_pixel_shows_banner_and_relaxes_csp(
    client: TestClient, db_session: Session
) -> None:
    pixel_shop(db_session)
    r = client.get("/form/nour")
    assert 'data-pixel="123456789012345"' in r.text and 'id="ads-consent"' in r.text
    assert "/static/pixel.js" in r.text and "<script>" not in r.text  # no inline script
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' https://unpkg.com https://connect.facebook.net" in csp
    assert "https://www.facebook.com" in csp and "unsafe-inline" not in csp
    done = client.get("/form/nour/done")
    assert 'data-event="Lead"' in done.text
    assert "connect.facebook.net" in done.headers["content-security-policy"]
    # The dashboard keeps the strict policy.
    assert "facebook" not in client.get("/login").headers["content-security-policy"]


def test_consented_order_keeps_browser_ids(client: TestClient, db_session: Session) -> None:
    pixel_shop(db_session)
    r = client.post("/form/nour", data=form(ad_consent="1", fbp=FBP, fbc=FBC),
                    headers={"user-agent": "Mozilla/5.0"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    order = db_session.scalar(select(Order))
    assert order is not None and order.ad_consent is True
    assert order.ad_meta["fbp"] == FBP and order.ad_meta["fbc"] == FBC
    assert order.ad_meta["ua"] == "Mozilla/5.0" and order.ad_meta["url"].endswith("/form/nour")


def test_no_consent_stores_nothing(client: TestClient, db_session: Session) -> None:
    pixel_shop(db_session)
    client.post("/form/nour", data=form(ad_consent="", fbp=FBP), follow_redirects=False)
    order = db_session.scalar(select(Order))
    assert order is not None and order.ad_consent is False and order.ad_meta is None


def test_consent_ignored_when_shop_has_no_pixel(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="nour", form_products=[{"name": "Robe", "price": "89.000"}])
    db_session.commit()
    client.post("/form/nour", data=form(ad_consent="1", fbp=FBP), follow_redirects=False)
    order = db_session.scalar(select(Order))
    assert order is not None and order.ad_consent is False and order.ad_meta is None


# -------------------------------------------------------------- CAPI job
def consented(db: Session, shop: Shop, status: OrderStatus, **kw: object) -> Order:
    return make_order(db, shop, make_customer(db, shop, phone_e164=f"+2165544{kw.pop('n', 4333)}"),
                      status, ad_consent=True, ad_meta={"fbp": FBP, "ua": "UA", "url": "u"},
                      created_at=NOW - timedelta(hours=1), **kw)  # fmt: skip


@respx.mock
def test_sends_only_confirmed_consented_orders(db_session: Session) -> None:
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(200, json={"events_received": 1, "fbtrace_id": "x"})
    )
    shop = pixel_shop(db_session, meta_test_event_code="TEST123")
    ok = consented(db_session, shop, OrderStatus.CONFIRMED, n=4001)
    consented(db_session, shop, OrderStatus.AWAITING_CONFIRMATION, n=4002)  # not yet
    consented(db_session, shop, OrderStatus.CANCELLED, n=4003)  # never
    make_order(db_session, shop, make_customer(db_session, shop, phone_e164="+21655444004"),
               OrderStatus.CONFIRMED, created_at=NOW - timedelta(hours=1))  # no consent  # fmt: skip
    make_order(db_session, shop, make_customer(db_session, shop, phone_e164="+21655444005"),
               OrderStatus.CONFIRMED, ad_consent=True, source=OrderSource.SHOPIFY,
               created_at=NOW - timedelta(hours=1))  # not from the form  # fmt: skip
    db_session.commit()

    assert send_pending_purchases(db_session, shop, NOW) == 1
    body = json.loads(route.calls.last.request.content)
    assert [e["event_id"] for e in body["data"]] == [f"purchase-{ok.id}"]
    assert body["test_event_code"] == "TEST123"
    assert route.calls.last.request.url.params["access_token"] == "EAAG-test-token"
    db_session.refresh(ok)
    assert ok.capi_state == "sent" and ok.capi_sent_at is not None
    assert send_pending_purchases(db_session, shop, NOW) == 0  # never twice
    assert route.call_count == 1


@respx.mock
def test_delivered_mode_waits_for_delivery(db_session: Session) -> None:
    respx.post(EVENTS_URL).mock(return_value=httpx.Response(200, json={"events_received": 1}))
    shop = pixel_shop(db_session, meta_event_on="delivered")
    confirmed = consented(db_session, shop, OrderStatus.CONFIRMED, n=4001)
    delivered = consented(db_session, shop, OrderStatus.DELIVERED, n=4002)
    db_session.commit()
    assert send_pending_purchases(db_session, shop, NOW) == 1
    assert delivered.capi_state == "sent" and confirmed.capi_state is None


@respx.mock
def test_server_error_is_retried_client_error_is_not(db_session: Session) -> None:
    route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(503))
    shop = pixel_shop(db_session)
    order = consented(db_session, shop, OrderStatus.CONFIRMED)
    db_session.commit()
    assert send_pending_purchases(db_session, shop, NOW) == 0
    assert order.capi_state is None  # retried next tick

    route.mock(return_value=httpx.Response(400, json={"error": {"code": 190, "message": "bad"}}))
    assert send_pending_purchases(db_session, shop, NOW) == 0
    assert order.capi_state == "error"  # misconfiguration: no endless loop
    assert send_pending_purchases(db_session, shop, NOW) == 0 and route.call_count == 2


@respx.mock
def test_old_orders_and_shops_without_token_are_skipped(db_session: Session) -> None:
    route = respx.post(EVENTS_URL).mock(return_value=httpx.Response(200, json={}))
    shop = pixel_shop(db_session)
    consented(db_session, shop, OrderStatus.CONFIRMED, n=4001)
    db_session.commit()
    assert send_pending_purchases(db_session, shop, NOW + timedelta(days=31)) == 0
    shop.meta_capi_token = None
    assert send_pending_purchases(db_session, shop, NOW) == 0
    assert route.call_count == 0


# ------------------------------------------------------------- settings
def login_owner(client: TestClient, db: Session, shop: Shop) -> str:
    db.add(User(email="o@b.tn", password_hash=hash_password("owner-password-1"),
                role=Role.OWNER, shop_id=shop.id))  # fmt: skip
    db.commit()
    client.post("/login", data={"email": "o@b.tn", "password": "owner-password-1"})
    return CSRF_RE.search(client.get("/dashboard/settings").text).group(1)  # type: ignore[union-attr]


def test_owner_configures_pixel(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, slug="nour")
    token = login_owner(client, db_session, shop)
    assert "Meta Pixel" in client.get("/dashboard/settings").text

    r = client.post("/dashboard/pixel", data={"csrf_token": token, "pixel_id": "123456789012345",
                    "capi_token": "EAAG-secret", "event_on": "delivered",
                    "test_event_code": "TEST1"}, follow_redirects=False)  # fmt: skip
    assert "msg=saved" in r.headers["location"]
    db_session.expire_all()
    assert shop.meta_pixel_id == "123456789012345" and shop.meta_event_on == "delivered"
    assert shop.meta_capi_token != "EAAG-secret"  # encrypted at rest
    assert reveal_secret(shop.meta_capi_token) == "EAAG-secret"
    assert "EAAG-secret" not in client.get("/dashboard/settings").text

    # Empty token field keeps the stored one.
    client.post("/dashboard/pixel", data={"csrf_token": token, "pixel_id": "123456789012345"})
    db_session.expire_all()
    assert reveal_secret(shop.meta_capi_token) == "EAAG-secret"

    bad = client.post("/dashboard/pixel", data={"csrf_token": token, "pixel_id": "abc"},
                      follow_redirects=False)  # fmt: skip
    assert "msg=pixel_invalid" in bad.headers["location"]

    client.post("/dashboard/pixel", data={"csrf_token": token, "disable": "on"})
    db_session.expire_all()
    assert shop.meta_pixel_id is None and shop.meta_capi_token is None


@respx.mock
def test_pixel_test_event(client: TestClient, db_session: Session) -> None:
    route = respx.post(EVENTS_URL).mock(
        return_value=httpx.Response(200, json={"events_received": 1})
    )
    shop = pixel_shop(db_session)
    token = login_owner(client, db_session, shop)
    r = client.post("/dashboard/pixel/test", data={"csrf_token": token}, follow_redirects=False)
    assert "msg=pixel_test_ok" in r.headers["location"]
    assert json.loads(route.calls.last.request.content)["data"][0]["event_name"] == "PageView"
    route.mock(return_value=httpx.Response(400, json={"error": {"code": 190}}))
    r = client.post("/dashboard/pixel/test", data={"csrf_token": token}, follow_redirects=False)
    assert "msg=pixel_test_err" in r.headers["location"]


def test_pixel_settings_require_owner(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, slug="nour")
    db_session.add(User(email="s@b.tn", password_hash=hash_password("staff-password-1"),
                        role=Role.STAFF, shop_id=shop.id))  # fmt: skip
    db_session.commit()
    client.post("/login", data={"email": "s@b.tn", "password": "staff-password-1"})
    assert client.post("/dashboard/pixel", data={"pixel_id": "123456789012345"}).status_code == 403
