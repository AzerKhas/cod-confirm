"""Subscriptions: checkout, payment confirmation, renewal reminders and auto-suspension.

Rules
* The price is always computed server-side (`plans.price_millimes`).
* A payment is applied once (idempotent): plan set, `paid_until` extended from the later of
  today and the current end date, shop re-activated, invoice number assigned.
* A payment whose verified amount is below the expected price is never applied.
* Shops past `paid_until` + grace days are suspended (data kept, nothing sent) and come back
  automatically when they pay. The dashboard stays accessible so they can pay.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Payment, Shop, User
from app.plans import PAID_PLANS, Plan, limits, price_millimes
from app.services.alerts import notify_owner
from app.services.payments import (
    CallbackUrls,
    PaymentProvider,
    PaymentProviderError,
    Verification,
    available_providers,
)

log = logging.getLogger(__name__)

VERIFY_EVERY = timedelta(minutes=10)
MAX_CHECKS = 6
PENDING_TTL = timedelta(hours=24)
REMIND_DAYS = (3, 0)  # days before paid_until when the owner is reminded


class BillingError(ValueError):
    """Invalid checkout request (plan, period or provider)."""


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def today_for(shop: Shop, now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo

    return (now or datetime.now(UTC)).astimezone(ZoneInfo(shop.timezone or "Africa/Tunis")).date()


@dataclass(frozen=True)
class BillingState:
    status: str  # trial | active | expiring | grace | suspended | unset
    days_left: int | None
    label: str

    @property
    def needs_attention(self) -> bool:
        return self.status in ("expiring", "grace", "suspended")


def billing_state(shop: Shop, today: date | None = None) -> BillingState:
    today = today or today_for(shop)
    if not shop.is_active:
        return BillingState("suspended", None, "Abonnement suspendu : les messages sont arrêtés.")
    if shop.paid_until is None:
        return BillingState("unset", None, "")
    days = (shop.paid_until - today).days
    trial = shop.plan == Plan.TRIAL.value
    if days < 0:
        grace = get_settings().billing_grace_days + days
        return BillingState(
            "grace", days, f"Abonnement expiré : suspension dans {max(grace, 0)} jour(s)."
        )
    if days <= 5:
        what = "L'essai gratuit" if trial else "Votre abonnement"
        return BillingState("expiring", days, f"{what} se termine dans {days} jour(s).")
    return BillingState("trial" if trial else "active", days, "")


# ------------------------------------------------------------------ checkout
def start_checkout(
    db: Session,
    shop: Shop,
    user: User | None,
    plan: str,
    months: int,
    provider_name: str,
    base_url: str,
    providers: dict[str, PaymentProvider] | None = None,
) -> Payment:
    try:
        plan_v = Plan(plan)
    except ValueError as exc:
        raise BillingError("unknown plan") from exc
    if plan_v not in PAID_PLANS:
        raise BillingError("this plan cannot be bought")
    providers = providers if providers is not None else available_providers()
    provider = providers.get(provider_name)
    if provider is None:
        raise BillingError("payment method not available")
    try:
        amount = price_millimes(plan_v, months)
    except ValueError as exc:
        raise BillingError("unsupported billing period") from exc

    payment = Payment(
        shop_id=shop.id,
        provider=provider.name,
        plan=plan_v.value,
        months=months,
        amount_millimes=amount,
        status="pending",
        created_by=user.id if user else None,
        checks=0,
    )
    db.add(payment)
    db.flush()  # we need payment.id for the callback URLs
    base = base_url.rstrip("/")
    urls = CallbackUrls(
        success=f"{base}/billing/{provider.name}/return?p={payment.id}&result=success",
        fail=f"{base}/billing/{provider.name}/return?p={payment.id}&result=fail",
        webhook=f"{base}/billing/{provider.name}/notify",
    )
    try:
        checkout = provider.create(payment, shop, urls)
    except PaymentProviderError:
        db.rollback()
        log.warning(
            "billing: checkout failed", extra={"shop_id": shop.id, "provider": provider.name}
        )
        raise
    payment.provider_ref = checkout.ref
    payment.pay_url = checkout.url
    db.commit()
    log.info(
        "billing: checkout started",
        extra={
            "shop_id": shop.id,
            "payment_id": payment.id,
            "provider": provider.name,
            "amount": amount,
        },
    )
    return payment


# --------------------------------------------------------------- confirmation
def apply_paid(
    db: Session, payment: Payment, now: datetime | None = None, note: str | None = None
) -> bool:
    """Extend the subscription. Returns False when the payment was already applied."""
    if payment.status == "paid":
        return False
    now = now or datetime.now(UTC)
    shop = db.get(Shop, payment.shop_id)
    assert shop is not None
    today = today_for(shop, now)
    start = max(today, shop.paid_until or today) if shop.plan != Plan.TRIAL.value else today
    end = add_months(start, payment.months)

    payment.status = "paid"
    payment.paid_at = now
    payment.period_start, payment.period_end = start, end
    if note:
        payment.note = note[:300]
    db.flush()
    payment.invoice_number = f"CC-{now.year}-{payment.id:06d}"
    shop.plan = payment.plan
    shop.paid_until = end
    shop.is_active = True
    db.commit()
    log.info(
        "billing: payment applied",
        extra={
            "shop_id": shop.id,
            "payment_id": payment.id,
            "plan": payment.plan,
            "paid_until": end.isoformat(),
        },
    )
    notify_owner(
        shop, f"✅ Paiement reçu : {limits(payment.plan).label} jusqu'au {end:%d/%m/%Y}. Merci !"
    )
    return True


def confirm(
    db: Session, payment: Payment, result: Verification, now: datetime | None = None
) -> str:
    """Apply a provider verification to our payment. Returns the payment status."""
    if payment.status != "pending":
        return payment.status
    if result.status == "paid":
        if result.amount_millimes is not None and result.amount_millimes < payment.amount_millimes:
            payment.status = "failed"
            payment.note = (
                f"amount mismatch: got {result.amount_millimes}, expected {payment.amount_millimes}"
            )
            db.commit()
            log.error(
                "billing: amount mismatch, payment NOT applied", extra={"payment_id": payment.id}
            )
            return payment.status
        apply_paid(db, payment, now)
    elif result.status in ("failed", "expired"):
        payment.status = result.status
        db.commit()
    return payment.status


def verify_payment(
    db: Session,
    payment: Payment,
    providers: dict[str, PaymentProvider] | None = None,
    now: datetime | None = None,
) -> str:
    if payment.status != "pending" or not payment.provider_ref:
        return payment.status
    providers = providers if providers is not None else available_providers()
    provider = providers.get(payment.provider)
    if provider is None:
        return payment.status
    payment.checks = (payment.checks or 0) + 1
    payment.checked_at = now or datetime.now(UTC)
    db.commit()
    try:
        result = provider.verify(payment.provider_ref)
    except PaymentProviderError:
        log.warning("billing: verification failed", extra={"payment_id": payment.id})
        return payment.status
    return confirm(db, payment, result, now)


def find_by_ref(db: Session, provider: str, ref: str) -> Payment | None:
    if not ref or len(ref) > 100:
        return None
    return db.scalar(
        select(Payment).where(Payment.provider == provider, Payment.provider_ref == ref)
    )


def record_manual(
    db: Session, shop: Shop, plan: str, months: int, amount_millimes: int, note: str, user: User
) -> Payment:
    """Bank transfer / cash received by the operator."""
    plan_v = Plan(plan)
    if plan_v not in PAID_PLANS or not 1 <= months <= 24 or amount_millimes < 0:
        raise BillingError("invalid manual payment")
    payment = Payment(
        shop_id=shop.id,
        provider="manual",
        plan=plan_v.value,
        months=months,
        amount_millimes=amount_millimes,
        status="pending",
        created_by=user.id,
        checks=0,
    )
    db.add(payment)
    db.flush()
    apply_paid(db, payment, note=note or "paiement manuel")
    return payment


# ------------------------------------------------------------- scheduled jobs
def billing_jobs(db: Session, shop: Shop, now: datetime) -> dict[str, int]:
    counts = {"verified": 0, "expired": 0, "reminders": 0, "suspended": 0}
    pending = db.scalars(
        select(Payment).where(Payment.shop_id == shop.id, Payment.status == "pending")
    ).all()
    for p in pending:
        created = p.created_at if p.created_at.tzinfo else p.created_at.replace(tzinfo=UTC)
        if now - created > PENDING_TTL:
            p.status = "expired"
            db.commit()
            counts["expired"] += 1
            continue
        last = p.checked_at or created
        last = last if last.tzinfo else last.replace(tzinfo=UTC)
        # A few spaced checks only: Flouci bans IPs that poll too often.
        if p.provider_ref and (p.checks or 0) < MAX_CHECKS and now - last >= VERIFY_EVERY:
            verify_payment(db, p, now=now)
            counts["verified"] += 1

    if shop.paid_until is None:
        return counts
    today = today_for(shop, now)
    days = (shop.paid_until - today).days
    if shop.billing_notice_on == today:
        return counts
    grace = get_settings().billing_grace_days
    if shop.is_active and days < -grace:
        shop.is_active = False
        shop.billing_notice_on = today
        db.commit()
        notify_owner(
            shop,
            "⛔ Votre abonnement COD Confirm a expiré : les confirmations WhatsApp sont "
            "arrêtées. Renouvelez depuis Tableau de bord → Abonnement.",
        )
        log.info("billing: shop suspended", extra={"shop_id": shop.id})
        counts["suspended"] += 1
    elif shop.is_active and (days in REMIND_DAYS or days < 0):
        shop.billing_notice_on = today
        db.commit()
        when = "aujourd'hui" if days == 0 else (f"dans {days} jours" if days > 0 else "déjà")
        notify_owner(
            shop,
            f"⏰ Votre abonnement COD Confirm se termine {when} ({shop.paid_until:%d/%m/%Y}). "
            "Renouvelez depuis Tableau de bord → Abonnement.",
        )
        counts["reminders"] += 1
    return counts


def trial_end(today: date) -> date:
    return today + timedelta(days=get_settings().trial_days)
