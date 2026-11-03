from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import OrderSource, OrderStatus
from app.models import Customer, Event, Order
from tests.factories import make_shop_with_key


def payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "customer_name": "Ahmed Ben Salah",
        "phone": "98 123 456",
        "address": "Rue Habib Bourguiba 10",
        "city": "Sousse",
        "items": [{"name": "Skmei 1251 noir", "qty": 2, "price": "59"}],
    }
    base.update(over)
    return base


@pytest.fixture
def shop_key(db_session: Session) -> tuple[Any, str]:
    shop, key = make_shop_with_key(db_session, slug="skmei-tn")
    db_session.commit()
    return shop, key


def test_create_order(client: TestClient, db_session: Session, shop_key: tuple[Any, str]) -> None:
    _shop, key = shop_key
    r = client.post("/api/orders", json=payload(), headers={"X-API-Key": key})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["status"] == "NEW"
    assert body["risk_warning"] is False

    order = db_session.get(Order, body["id"])
    assert order is not None
    assert order.customer.phone_e164 == "+21698123456"
    assert order.total == Decimal("118.000")  # computed: 2 x 59
    assert order.items == [{"name": "Skmei 1251 noir", "qty": 2, "price": "59.000"}]
    assert order.source is OrderSource.API
    # The confirmation was sent in the background (DRY_RUN) right after the response.
    db_session.expire_all()
    assert order.status is OrderStatus.AWAITING_CONFIRMATION
    assert [e.to_status for e in order.events] == [
        OrderStatus.NEW,
        OrderStatus.AWAITING_CONFIRMATION,
    ]
    assert len(order.messages) == 1
    assert order.messages[0].content.startswith("Salam Ahmed")


def test_given_total_is_kept(
    client: TestClient, shop_key: tuple[Any, str], db_session: Session
) -> None:
    r = client.post("/api/orders", json=payload(total="125.5"), headers={"X-API-Key": shop_key[1]})
    assert r.status_code == 201
    assert db_session.get(Order, r.json()["id"]).total == Decimal("125.500")  # type: ignore[union-attr]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "cc_wrong"}, {"X-API-Key": "x" * 500}])
def test_auth_required(
    client: TestClient, shop_key: tuple[Any, str], headers: dict[str, str]
) -> None:
    r = client.post("/api/orders", json=payload(), headers=headers)
    assert r.status_code == 401


@pytest.mark.parametrize(
    "bad",
    [
        {"phone": "71 123 456"},  # landline
        {"phone": "123"},
        {"items": []},
        {"items": [{"name": "x", "qty": 0, "price": "1"}]},
        {"items": [{"name": "x", "qty": 1, "price": "-5"}]},
        {"customer_name": ""},
        {"customer_name": "a" * 500},
        {"currency": "dinar"},
        {"total": "-1"},
    ],
)
def test_validation(client: TestClient, shop_key: tuple[Any, str], bad: dict[str, Any]) -> None:
    r = client.post("/api/orders", json=payload(**bad), headers={"X-API-Key": shop_key[1]})
    assert r.status_code == 422


def test_duplicate_external_id_is_idempotent(
    client: TestClient, db_session: Session, shop_key: tuple[Any, str]
) -> None:
    h = {"X-API-Key": shop_key[1]}
    first = client.post("/api/orders", json=payload(external_id="A-1"), headers=h)
    second = client.post("/api/orders", json=payload(external_id="A-1"), headers=h)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["id"] == first.json()["id"]
    assert db_session.scalar(select(func.count()).select_from(Order)) == 1


def test_same_customer_reused(
    client: TestClient, db_session: Session, shop_key: tuple[Any, str]
) -> None:
    h = {"X-API-Key": shop_key[1]}
    client.post("/api/orders", json=payload(phone="98123456"), headers=h)
    client.post(
        "/api/orders", json=payload(phone="+216 98 123 456", customer_name="Ahmed B."), headers=h
    )
    customers = db_session.scalars(select(Customer)).all()
    assert len(customers) == 1
    assert customers[0].total_orders == 2
    assert customers[0].name == "Ahmed B."
    # 2 orders x (NEW + AWAITING_CONFIRMATION after the background send)
    assert db_session.scalar(select(func.count()).select_from(Event)) == 4


def test_shops_are_isolated(client: TestClient, db_session: Session) -> None:
    shop_a, key_a = make_shop_with_key(db_session)
    shop_b, key_b = make_shop_with_key(db_session)
    db_session.commit()
    client.post("/api/orders", json=payload(external_id="1"), headers={"X-API-Key": key_a})
    r = client.post("/api/orders", json=payload(external_id="1"), headers={"X-API-Key": key_b})
    assert r.status_code == 201  # same external id, other shop -> new order
    assert {o.shop_id for o in db_session.scalars(select(Order))} == {shop_a.id, shop_b.id}
