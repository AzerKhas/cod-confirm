"""Order intake: one idempotent entry point + mappers for Shopify / WooCommerce payloads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import OrderSource
from app.models import Customer, Order, Shop
from app.schemas import OrderIn
from app.security import mask_phone
from app.services.orders import record_new_order

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntakeResult:
    order: Order
    created: bool


class SkipOrder(Exception):
    """The webhook payload is valid but must not be confirmed (e.g. already paid)."""


def _find_external(db: Session, shop: Shop, source: OrderSource, external_id: str) -> Order | None:
    return db.scalar(
        select(Order).where(
            Order.shop_id == shop.id, Order.source == source, Order.external_id == external_id
        )
    )


def _get_or_create_customer(db: Session, shop: Shop, data: OrderIn) -> Customer:
    customer = db.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.phone_e164 == data.phone)
    )
    if customer is None:
        customer = Customer(shop=shop, name=data.customer_name, phone_e164=data.phone)
        db.add(customer)
    else:
        customer.name = data.customer_name  # keep the latest name given
    if data.language is not None:
        customer.language = data.language
    return customer


def create_order(db: Session, shop: Shop, data: OrderIn, source: OrderSource) -> IntakeResult:
    """Create customer (if new) + order, log the NEW event and commit.

    Idempotent: an order with the same (shop, source, external_id) is returned as is.
    """
    if data.external_id:
        existing = _find_external(db, shop, source, data.external_id)
        if existing is not None:
            return IntakeResult(existing, created=False)

    try:
        with db.begin_nested():  # savepoint: a concurrent duplicate only rolls back this part
            customer = _get_or_create_customer(db, shop, data)
            order = Order(
                shop=shop,
                customer=customer,
                external_id=data.external_id,
                items=[i.as_json() for i in data.items],
                total=data.computed_total(),
                currency=data.currency,
                address=data.address,
                city=data.city,
                source=source,
            )
            db.add(order)
            db.flush()
            record_new_order(db, order)
    except IntegrityError:
        if data.external_id:
            existing = _find_external(db, shop, source, data.external_id)
            if existing is not None:
                return IntakeResult(existing, created=False)
        raise

    db.commit()
    log.info(
        "intake: order created",
        extra={
            "shop_id": shop.id,
            "order_id": order.id,
            "source": source.value,
            "phone": mask_phone(data.phone),
        },
    )
    return IntakeResult(order, created=True)


# ---------------------------------------------------------------- Shopify
def _join(*parts: Any) -> str:
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def order_in_from_shopify(p: dict[str, Any]) -> OrderIn:
    """Map a Shopify `orders/create` payload. Raises SkipOrder / pydantic.ValidationError."""
    if p.get("financial_status") in {"paid", "refunded", "voided"}:
        raise SkipOrder(f"financial_status={p.get('financial_status')}")

    ship = p.get("shipping_address") or {}
    bill = p.get("billing_address") or {}
    cust = p.get("customer") or {}
    addr = ship or bill
    phone = ship.get("phone") or bill.get("phone") or p.get("phone") or cust.get("phone") or ""
    name = _join(addr.get("first_name"), addr.get("last_name")) or _join(
        cust.get("first_name"), cust.get("last_name")
    )

    return OrderIn.model_validate(
        {
            "external_id": str(p.get("id", "")) or None,
            "customer_name": name,
            "phone": phone,
            "address": _join(addr.get("address1"), addr.get("address2")),
            "city": addr.get("city") or addr.get("province") or "",
            "items": [
                {"name": li.get("title") or li.get("name"), "qty": li.get("quantity"),
                 "price": li.get("price")}
                for li in p.get("line_items") or []
            ],
            "total": p.get("total_price"),
            "currency": p.get("currency") or "TND",
        }
    )


# ------------------------------------------------------------ WooCommerce
def order_in_from_woocommerce(p: dict[str, Any]) -> OrderIn:
    """Map a WooCommerce `order.created` payload. Only Cash-On-Delivery orders are kept."""
    if p.get("payment_method") and p.get("payment_method") != "cod":
        raise SkipOrder(f"payment_method={p.get('payment_method')}")

    bill = p.get("billing") or {}
    ship = p.get("shipping") or {}
    addr = ship if ship.get("address_1") else bill
    phone = bill.get("phone") or ship.get("phone") or ""
    name = _join(addr.get("first_name"), addr.get("last_name")) or _join(
        bill.get("first_name"), bill.get("last_name")
    )

    return OrderIn.model_validate(
        {
            "external_id": str(p.get("id", "")) or None,
            "customer_name": name,
            "phone": phone,
            "address": _join(addr.get("address_1"), addr.get("address_2")),
            "city": addr.get("city") or addr.get("state") or "",
            "items": [
                {"name": li.get("name"), "qty": li.get("quantity"), "price": li.get("price")}
                for li in p.get("line_items") or []
            ],
            "total": p.get("total"),
            "currency": p.get("currency") or "TND",
        }
    )
