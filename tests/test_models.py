from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.enums import Direction, MessageType, OrderSource, OrderStatus
from app.models import Customer, Event, MessageLog, Order
from app.security import generate_api_key, verify_api_key
from app.services.privacy import delete_customer, purge_expired
from tests.factories import make_customer, make_order, make_shop


def test_defaults(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    assert shop.default_language == "darija"
    assert shop.timezone == "Africa/Tunis"
    assert shop.reminder_delays == [3, 24]
    assert order.currency == "TND"
    assert order.status is OrderStatus.NEW
    assert order.created_at.tzinfo is not None  # always tz-aware


def test_customer_phone_unique_per_shop(db_session: Session) -> None:
    shop = make_shop(db_session)
    make_customer(db_session, shop, phone_e164="+21698000001")
    make_customer(db_session, make_shop(db_session), phone_e164="+21698000001")  # other shop ok
    with pytest.raises(IntegrityError):
        make_customer(db_session, shop, phone_e164="+21698000001")


def test_external_order_is_idempotent(db_session: Session) -> None:
    shop = make_shop(db_session)
    make_order(db_session, shop, source=OrderSource.SHOPIFY, external_id="1001")
    with pytest.raises(IntegrityError):
        make_order(db_session, shop, source=OrderSource.SHOPIFY, external_id="1001")


def test_invalid_enum_value_rejected(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    order.status = "HACKED"  # type: ignore[assignment]
    with pytest.raises((StatementError, LookupError, ValueError)):
        db_session.flush()


def test_naive_datetime_rejected(db_session: Session) -> None:
    order = make_order(db_session, make_shop(db_session))
    order.last_contacted_at = datetime(2026, 1, 1, 12, 0)  # naive
    with pytest.raises(StatementError):
        db_session.flush()


def test_reprs_hide_personal_data(db_session: Session) -> None:
    shop = make_shop(db_session)
    customer = make_customer(db_session, shop, phone_e164="+21698123456")
    assert "98123" not in repr(customer)
    assert "api_key" not in repr(shop)


def test_api_key_hash_roundtrip() -> None:
    plain, key_hash, prefix = generate_api_key()
    assert plain.startswith("cc_") and prefix == plain[:10]
    assert verify_api_key(plain, key_hash)
    assert not verify_api_key(plain + "x", key_hash)
    assert plain not in key_hash


def test_delete_customer_cascades(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    db_session.add(
        MessageLog(order=order, direction=Direction.OUT, type=MessageType.TEMPLATE, content="hi")
    )
    db_session.flush()

    delete_customer(db_session, order.customer)
    db_session.expire_all()

    for model in (Customer, Order, MessageLog, Event):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0


def test_purge_expired_keeps_active_orders(db_session: Session) -> None:
    shop = make_shop(db_session, data_retention_days=30)
    now = datetime.now(UTC)
    old = now - timedelta(days=40)

    old_done = make_order(db_session, shop, status=OrderStatus.DELIVERED, created_at=old)
    old_active = make_order(db_session, shop, status=OrderStatus.CONFIRMED, created_at=old)
    recent_done = make_order(db_session, shop, status=OrderStatus.CANCELLED)
    old_done_id = old_done.id
    old_done_customer_id = old_done.customer_id

    deleted = purge_expired(db_session, shop, now=now)

    assert deleted == 1
    remaining = set(db_session.scalars(select(Order.id)).all())
    assert remaining == {old_active.id, recent_done.id}
    assert old_done_id not in remaining
    # customer with no order left is removed too
    assert db_session.get(Customer, old_done_customer_id) is None
