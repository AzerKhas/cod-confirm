"""Monthly usage counters and quota checks."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Shop, UsageCounter
from app.plans import hard_quota


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def counter(db: Session, shop_id: int, month: str | None = None) -> UsageCounter:
    month = month or month_key()
    row = db.scalar(
        select(UsageCounter).where(UsageCounter.shop_id == shop_id, UsageCounter.month == month)
    )
    if row is None:
        row = UsageCounter(shop_id=shop_id, month=month, orders=0, messages_sent=0)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:  # created concurrently
            row = db.scalars(
                select(UsageCounter).where(
                    UsageCounter.shop_id == shop_id, UsageCounter.month == month
                )
            ).one()
    return row


def bump(db: Session, shop_id: int, *, orders: int = 0, messages: int = 0) -> None:
    row = counter(db, shop_id)
    row.orders += orders
    row.messages_sent += messages


def can_send(db: Session, shop: Shop) -> tuple[bool, str | None]:
    """May the bot message this shop's customers right now?"""
    if not shop.is_active:
        return False, "shop suspended"
    if counter(db, shop.id).orders > hard_quota(shop.plan):
        return False, "monthly quota exceeded"
    return True, None
