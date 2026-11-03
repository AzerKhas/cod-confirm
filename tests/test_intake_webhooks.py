from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import OrderSource
from app.models import Order
from app.security import hmac_sha256_base64
from tests.factories import make_shop

SHOPIFY_SECRET = "shpss_test_secret"
WOO_SECRET = "woo_test_secret"


@pytest.fixture
def shop(db_session: Session) -> Any:
    s = make_shop(
        db_session,
        slug="skmei-tn",
        shopify_webhook_secret=SHOPIFY_SECRET,
        woocommerce_webhook_secret=WOO_SECRET,
    )
    db_session.commit()
    return s


def shopify_order(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": 820982911946154508,
        "financial_status": "pending",
        "total_price": "126.00",
        "currency": "TND",
        "phone": None,
        "customer": {"first_name": "Sana", "last_name": "Trabelsi", "phone": "+21652111222"},
        "shipping_address": {
            "first_name": "Sana",
            "last_name": "Trabelsi",
            "address1": "Cite El Khadra",
            "address2": "Imm 4",
            "city": "Tunis",
            "phone": "52 111 222",
        },
        "line_items": [
            {"title": "Skmei 1335", "quantity": 1, "price": "69.00"},
            {"title": "Skmei 1251", "quantity": 1, "price": "57.00"},
        ],
    }
    p.update(over)
    return p


def woo_order(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": 727,
        "status": "processing",
        "currency": "TND",
        "total": "59.000",
        "payment_method": "cod",
        "billing": {"first_name": "Karim", "last_name": "Jaziri", "address_1": "Route de Gabes km 3",
                    "city": "Sfax", "phone": "0021697555666"},
        "shipping": {"first_name": "", "last_name": "", "address_1": "", "city": ""},
        "line_items": [{"name": "Skmei 1251", "quantity": 1, "price": 59}],
    }  # fmt: skip
    p.update(over)
    return p


def post_shopify(client: TestClient, body: dict[str, Any] | bytes, *, secret: str = SHOPIFY_SECRET,
                 topic: str = "orders/create", slug: str = "skmei-tn") -> Any:  # fmt: skip
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post(
        f"/webhooks/shopify/{slug}",
        content=raw,
        headers={
            "X-Shopify-Hmac-Sha256": hmac_sha256_base64(secret, raw),
            "X-Shopify-Topic": topic,
            "Content-Type": "application/json",
        },
    )


def post_woo(client: TestClient, body: dict[str, Any] | bytes, *, secret: str = WOO_SECRET,
             topic: str = "order.created") -> Any:  # fmt: skip
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post(
        "/webhooks/woocommerce/skmei-tn",
        content=raw,
        headers={
            "X-WC-Webhook-Signature": hmac_sha256_base64(secret, raw),
            "X-WC-Webhook-Topic": topic,
            "Content-Type": "application/json",
        },
    )


def count_orders(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Order)) or 0


# ------------------------------------------------------------------ Shopify
def test_shopify_creates_order(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_shopify(client, shopify_order())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created"

    order = db_session.get(Order, r.json()["order_id"])
    assert order is not None
    assert order.source is OrderSource.SHOPIFY
    assert order.external_id == "820982911946154508"
    assert order.customer.phone_e164 == "+21652111222"
    assert order.customer.name == "Sana Trabelsi"
    assert order.address == "Cite El Khadra Imm 4"
    assert order.city == "Tunis"
    assert order.total == Decimal("126.000")
    assert len(order.items) == 2


def test_shopify_retry_is_duplicate(client: TestClient, db_session: Session, shop: Any) -> None:
    post_shopify(client, shopify_order())
    r = post_shopify(client, shopify_order())
    assert r.json()["status"] == "duplicate"
    assert count_orders(db_session) == 1


def test_shopify_bad_signature(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_shopify(client, shopify_order(), secret="wrong")
    assert r.status_code == 401
    assert count_orders(db_session) == 0


def test_shopify_tampered_body(client: TestClient, db_session: Session, shop: Any) -> None:
    raw = json.dumps(shopify_order()).encode()
    sig = hmac_sha256_base64(SHOPIFY_SECRET, raw)
    tampered = raw.replace(b"126.00", b"1.00")
    r = client.post("/webhooks/shopify/skmei-tn", content=tampered,
                    headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"})  # fmt: skip
    assert r.status_code == 401


def test_shopify_unknown_shop_and_missing_secret(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="no-secret")
    db_session.commit()
    assert post_shopify(client, shopify_order(), slug="nope").status_code == 401
    assert post_shopify(client, shopify_order(), slug="no-secret").status_code == 401


def test_shopify_other_topic_ignored(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_shopify(client, shopify_order(), topic="orders/updated")
    assert r.json()["status"] == "ignored"
    assert count_orders(db_session) == 0


def test_shopify_paid_order_ignored(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_shopify(client, shopify_order(financial_status="paid"))
    assert r.json()["status"] == "ignored"
    assert count_orders(db_session) == 0


def test_shopify_invalid_phone_rejected_with_200(client: TestClient, db_session: Session,
                                                 shop: Any) -> None:  # fmt: skip
    p = shopify_order(customer={}, shipping_address={**shopify_order()["shipping_address"],
                                                     "phone": "71 000 000"})  # fmt: skip
    r = post_shopify(client, p)
    assert r.status_code == 200  # no endless retries from Shopify
    assert r.json()["status"] == "rejected"
    assert "phone" in r.json()["reason"]
    assert count_orders(db_session) == 0


def test_shopify_not_json(client: TestClient, shop: Any) -> None:
    assert post_shopify(client, b"not json").status_code == 400


def test_shopify_body_too_large(client: TestClient, shop: Any) -> None:
    assert post_shopify(client, b"x" * 1_000_001).status_code == 413


# -------------------------------------------------------------- WooCommerce
def test_woo_creates_order(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_woo(client, woo_order())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created"
    order = db_session.get(Order, r.json()["order_id"])
    assert order is not None
    assert order.source is OrderSource.WOOCOMMERCE
    assert order.customer.phone_e164 == "+21697555666"
    assert order.address == "Route de Gabes km 3"  # billing used when shipping empty
    assert order.city == "Sfax"


def test_woo_ping(client: TestClient, shop: Any) -> None:
    r = client.post("/webhooks/woocommerce/skmei-tn", content=b"webhook_id=12",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert r.json()["status"] == "ping"


def test_woo_bad_signature(client: TestClient, db_session: Session, shop: Any) -> None:
    assert post_woo(client, woo_order(), secret="nope").status_code == 401
    assert count_orders(db_session) == 0


def test_woo_non_cod_ignored(client: TestClient, db_session: Session, shop: Any) -> None:
    r = post_woo(client, woo_order(payment_method="stripe"))
    assert r.json()["status"] == "ignored"
    assert count_orders(db_session) == 0


def test_woo_other_topic_ignored(client: TestClient, shop: Any) -> None:
    assert post_woo(client, woo_order(), topic="order.updated").json()["status"] == "ignored"


def test_woo_duplicate(client: TestClient, db_session: Session, shop: Any) -> None:
    post_woo(client, woo_order())
    assert post_woo(client, woo_order()).json()["status"] == "duplicate"
    assert count_orders(db_session) == 1
