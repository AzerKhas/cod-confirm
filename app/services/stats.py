"""KPIs for a shop over a period (dashboard + daily summary)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import OrderStatus
from app.models import Order, Shop

S = OrderStatus
CONFIRMED_OR_LATER = (S.CONFIRMED, S.SHIPPED, S.DELIVERED, S.REFUSED)
PENDING = (S.NEW, S.AWAITING_CONFIRMATION, S.MODIFY_REQUESTED)


@dataclass(frozen=True)
class ShopStats:
    total: int
    by_status: dict[OrderStatus, int]
    revenue_confirmed: Decimal  # value of confirmed-or-later orders
    delivery_fee: Decimal

    def _n(self, *statuses: OrderStatus) -> int:
        return sum(self.by_status.get(s, 0) for s in statuses)

    @staticmethod
    def _rate(part: int, whole: int) -> float:
        return round(100 * part / whole, 1) if whole else 0.0

    @property
    def confirmed(self) -> int:
        return self._n(*CONFIRMED_OR_LATER)

    @property
    def cancelled(self) -> int:
        return self._n(S.CANCELLED)

    @property
    def no_answer(self) -> int:
        return self._n(S.NO_ANSWER)

    @property
    def pending(self) -> int:
        return self._n(*PENDING)

    @property
    def confirmation_rate(self) -> float:
        return self._rate(self.confirmed, self.total)

    @property
    def cancellation_rate(self) -> float:
        return self._rate(self.cancelled, self.total)

    @property
    def no_answer_rate(self) -> float:
        return self._rate(self.no_answer, self.total)

    @property
    def refusal_rate(self) -> float:
        """Refused at the door, among shipped orders that reached an outcome."""
        return self._rate(self._n(S.REFUSED), self._n(S.DELIVERED, S.REFUSED))

    @property
    def refusals_avoided(self) -> int:
        """Orders stopped before shipping (cancelled or never answered)."""
        return self.cancelled + self.no_answer

    @property
    def money_saved(self) -> Decimal:
        """Estimate: each order stopped before shipping saves one delivery fee."""
        return (self.delivery_fee * self.refusals_avoided).quantize(Decimal("0.001"))


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, zone)
    return start, start + timedelta(days=1)


def compute_stats(db: Session, shop: Shop, start: datetime, end: datetime) -> ShopStats:
    where = (Order.shop_id == shop.id, Order.created_at >= start, Order.created_at < end)
    rows = db.execute(select(Order.status, func.count()).where(*where).group_by(Order.status))
    by_status = {OrderStatus(s): int(n) for s, n in rows}
    revenue = db.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(
            *where, Order.status.in_(CONFIRMED_OR_LATER)
        )
    )
    return ShopStats(
        total=sum(by_status.values()),
        by_status=by_status,
        revenue_confirmed=Decimal(str(revenue or 0)).quantize(Decimal("0.001")),
        delivery_fee=shop.delivery_fee,
    )
