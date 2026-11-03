"""Actions of the shop owner on an order (dashboard)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Actor, MessageStatus, OrderStatus
from app.models import Order, Shop
from app.schemas import OrderItemIn
from app.services import messages as m
from app.services.notify import send_order_template
from app.services.orders import transition

log = logging.getLogger(__name__)
S = OrderStatus

# Status buttons shown in the dashboard for each current status.
ADMIN_ACTIONS: dict[OrderStatus, tuple[OrderStatus, ...]] = {
    S.NEW: (S.CANCELLED,),
    S.AWAITING_CONFIRMATION: (S.CONFIRMED, S.CANCELLED),
    S.MODIFY_REQUESTED: (S.CANCELLED,),
    S.NO_ANSWER: (S.CONFIRMED, S.CANCELLED),
    S.CONFIRMED: (S.SHIPPED, S.CANCELLED),
    S.SHIPPED: (S.DELIVERED, S.REFUSED),
    S.DELIVERED: (),
    S.REFUSED: (),
    S.CANCELLED: (),
}


def set_status(db: Session, order: Order, to: OrderStatus, note: str | None = None) -> None:
    """Admin status change; SHIPPED also queues the 'order shipped' WhatsApp notice."""
    transition(db, order, to, Actor.ADMIN, note=note)
    if to is S.SHIPPED:
        order.shipped_notified_at = None  # sent by send_shipped_notice / the scheduler
    db.commit()


def send_shipped_notice(db: Session, order: Order) -> bool:
    if order.status is not S.SHIPPED or order.shipped_notified_at is not None:
        return False
    msg = send_order_template(db, order, m.ORDER_SHIPPED)
    if msg is None:  # opted out: never retry
        order.shipped_notified_at = datetime.now(UTC)
    elif msg.status is MessageStatus.SENT:
        order.shipped_notified_at = datetime.now(UTC)
    db.commit()
    return msg is not None and msg.status is MessageStatus.SENT


def pending_shipped_notices(db: Session, shop: Shop) -> list[Order]:
    return list(
        db.scalars(
            select(Order).where(
                Order.shop_id == shop.id,
                Order.status == S.SHIPPED,
                Order.shipped_notified_at.is_(None),
            )
        )
    )


def edit_order(
    db: Session,
    order: Order,
    *,
    address: str,
    city: str,
    items: list[OrderItemIn],
    total: Decimal | None,
) -> None:
    """Edit an order. A MODIFY_REQUESTED order goes back to AWAITING_CONFIRMATION and the
    customer receives the confirmation again (with the new details)."""
    if order.status in (S.SHIPPED, S.DELIVERED, S.REFUSED, S.CANCELLED):
        raise ValueError("this order can no longer be edited")
    order.sheet_dirty = True
    order.address = address
    order.city = city
    order.items = [i.as_json() for i in items]
    order.total = (
        total
        if total is not None
        else sum((i.price * i.qty for i in items), Decimal(0)).quantize(Decimal("0.001"))
    )

    if order.status is S.MODIFY_REQUESTED:
        transition(db, order, S.AWAITING_CONFIRMATION, Actor.ADMIN, note="order edited")
        order.reminders_sent = 0
        send_order_template(db, order, m.ORDER_CONFIRMATION)
    db.commit()


def resend_confirmation(db: Session, order: Order) -> bool:
    """Send the confirmation template again (NO_ANSWER / AWAITING / NEW)."""
    if order.status not in (S.NEW, S.AWAITING_CONFIRMATION, S.NO_ANSWER):
        return False
    msg = send_order_template(db, order, m.ORDER_CONFIRMATION)
    if msg is None or msg.status is not MessageStatus.SENT:
        db.commit()
        return False
    if order.status is not S.AWAITING_CONFIRMATION:
        transition(db, order, S.AWAITING_CONFIRMATION, Actor.ADMIN, note="confirmation re-sent")
    order.reminders_sent = 0
    db.commit()
    return True
