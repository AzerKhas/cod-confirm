"""Small helpers to build test data."""

from __future__ import annotations

from decimal import Decimal
from itertools import count

from sqlalchemy.orm import Session

from app.enums import OrderSource, OrderStatus
from app.models import Customer, Order, Shop
from app.security import generate_api_key
from app.services.orders import record_new_order

_seq = count(1)


def make_shop(db: Session, **kw: object) -> Shop:
    return make_shop_with_key(db, **kw)[0]


def make_shop_with_key(db: Session, **kw: object) -> tuple[Shop, str]:
    """Return (shop, plain API key)."""
    n = next(_seq)
    plain, key_hash, prefix = generate_api_key()
    shop = Shop(
        name=kw.pop("name", f"Shop {n}"),
        slug=kw.pop("slug", f"shop-{n}"),
        api_key_hash=key_hash,
        api_key_prefix=prefix,
        **kw,
    )
    db.add(shop)
    db.flush()
    return shop, plain


def make_customer(db: Session, shop: Shop, **kw: object) -> Customer:
    n = next(_seq)
    customer = Customer(
        shop=shop,
        name=kw.pop("name", f"Client {n}"),
        phone_e164=kw.pop("phone_e164", f"+2169{n:07d}"),
        **kw,
    )
    db.add(customer)
    db.flush()
    return customer


def make_order(
    db: Session,
    shop: Shop,
    customer: Customer | None = None,
    status: OrderStatus = OrderStatus.NEW,
    **kw: object,
) -> Order:
    customer = customer or make_customer(db, shop)
    order = Order(
        shop=shop,
        customer=customer,
        items=kw.pop("items", [{"name": "Skmei 1251", "qty": 1, "price": "59.000"}]),
        total=kw.pop("total", Decimal("59.000")),
        address=kw.pop("address", "Rue de la Liberte 12"),
        city=kw.pop("city", "Sfax"),
        source=kw.pop("source", OrderSource.FORM),
        **kw,
    )
    db.add(order)
    db.flush()
    record_new_order(db, order)
    order.status = status  # jump directly to a given state for tests
    db.flush()
    return order
