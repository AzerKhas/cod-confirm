"""Personal-data management: delete a customer, purge data past the retention period."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Order, Shop
from app.services.orders import TERMINAL

log = logging.getLogger(__name__)


def delete_customer(db: Session, customer: Customer) -> None:
    """Hard-delete a customer with all their orders, messages and events."""
    customer_id, shop_id = customer.id, customer.shop_id
    db.delete(customer)
    db.flush()
    log.info("privacy: customer deleted", extra={"customer_id": customer_id, "shop_id": shop_id})


def purge_expired(db: Session, shop: Shop, now: datetime | None = None) -> int:
    """Delete finished orders older than the shop's retention period, then
    customers left without any order. Active orders are never purged.

    Returns the number of deleted orders.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=shop.data_retention_days)
    old_orders = db.scalars(
        select(Order).where(
            Order.shop_id == shop.id,
            Order.created_at < cutoff,
            Order.status.in_(TERMINAL),
        )
    ).all()
    for order in old_orders:
        db.delete(order)
    db.flush()

    orphans = db.scalars(
        select(Customer).where(Customer.shop_id == shop.id, ~Customer.orders.any())
    ).all()
    for customer in orphans:
        db.delete(customer)
    db.flush()

    log.info(
        "privacy: retention purge",
        extra={"shop_id": shop.id, "orders": len(old_orders), "customers": len(orphans)},
    )
    return len(old_orders)
