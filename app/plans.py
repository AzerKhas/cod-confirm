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
