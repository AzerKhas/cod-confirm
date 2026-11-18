"""Subscription plans (by monthly order volume) and their limits.

Prices are the operator's catalogue (TND / month) and only used for display.
When a shop goes over its monthly quota (+10% tolerance), orders are still saved (never
lose a customer's order) but no WhatsApp message is sent until the next month or an upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Plan(StrEnum):
    TRIAL = "trial"
    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"


@dataclass(frozen=True)
class PlanLimits:
    label: str
    monthly_orders: int
    price_tnd: int
    api_requests_per_minute: int


PLANS: dict[Plan, PlanLimits] = {
    Plan.TRIAL: PlanLimits("Essai", 50, 0, 30),
    Plan.STARTER: PlanLimits("Starter", 300, 49, 60),
    Plan.PRO: PlanLimits("Pro", 1500, 129, 300),
    Plan.BUSINESS: PlanLimits("Business", 6000, 349, 1200),
}

QUOTA_TOLERANCE = 1.10


def limits(plan: Plan | str) -> PlanLimits:
    return PLANS[Plan(plan)]


def hard_quota(plan: Plan | str) -> int:
    return int(limits(plan).monthly_orders * QUOTA_TOLERANCE)


# Billing periods (months) and their discount: 12 months = 10 months paid.
DURATIONS: dict[int, tuple[str, float]] = {
    1: ("1 mois", 1.0),
    3: ("3 mois", 0.95),
    12: ("12 mois", 10 / 12),
}
PAID_PLANS: tuple[Plan, ...] = (Plan.STARTER, Plan.PRO, Plan.BUSINESS)


def price_millimes(plan: Plan | str, months: int) -> int:
    """Total price in millimes (TND x 1000), rounded to the dinar."""
    if months not in DURATIONS:
        raise ValueError("unsupported billing period")
    monthly = limits(plan).price_tnd
    if monthly <= 0:
        raise ValueError("this plan cannot be bought")
    return round(monthly * months * DURATIONS[months][1]) * 1000
