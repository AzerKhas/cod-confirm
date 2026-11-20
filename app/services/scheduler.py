"""Scheduled jobs: pending confirmations, reminders, NO_ANSWER timeout, daily summary.

`tick(now)` runs every job once for every shop; APScheduler calls it every
SCHEDULER_INTERVAL_SECONDS. Every job is idempotent, so a missed or repeated tick is
harmless. Everything is timezone-aware (shop.timezone, default Africa/Tunis).

Quiet hours (default 22:00-08:00 local): no business-initiated message (confirmation
template, reminders) is sent; they go out at the next allowed tick. Replies to a message
the customer just sent are not affected (the customer started the conversation).

Run the scheduler in ONE process only (SCHEDULER_ENABLED=true on a single instance).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import Actor, Direction, MessageStatus, MessageType, OrderStatus
from app.models import Event, MessageLog, Order, Shop
from app.services import messages as m
from app.services.alerts import notify_owner
from app.services.billing import billing_jobs
from app.services.meta_capi import send_pending_purchases
from app.services.notify import send_order_confirmation, send_order_template
from app.services.orders import transition
from app.services.privacy import purge_expired
from app.services.sheets import sync_shop
from app.services.stats import compute_stats, local_day_bounds

log = logging.getLogger(__name__)
S = OrderStatus


# ------------------------------------------------------------------- time
def local_now(shop: Shop, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(shop.timezone or "Africa/Tunis"))


def is_quiet_hours(shop: Shop, now: datetime, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    hour = local_now(shop, now).hour
    start, end = s.quiet_hours_start, s.quiet_hours_end
    if start == end:
        return False
    if start > end:  # range crosses midnight, e.g. 22 -> 8
        return hour >= start or hour < end
    return start <= hour < end


# ------------------------------------------------------------------- jobs
def _failed_template_sends(db: Session, order: Order) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(MessageLog)
            .where(
                MessageLog.order_id == order.id,
                MessageLog.direction == Direction.OUT,
                MessageLog.type == MessageType.TEMPLATE,
                MessageLog.status == MessageStatus.FAILED,
            )
        )
        or 0
    )


def send_pending_confirmations(db: Session, shop: Shop, now: datetime) -> int:
    """NEW orders not confirmed yet (night orders, failed sends, missed background job)."""
    settings = get_settings()
    if is_quiet_hours(shop, now, settings):
        return 0
    orders = db.scalars(
        select(Order).where(Order.shop_id == shop.id, Order.status == S.NEW).order_by(Order.id)
    ).all()
    sent = 0
    for order in orders:
        if order.customer.opted_out:
            continue
        if _failed_template_sends(db, order) >= settings.max_send_failures:
            continue  # visible in the dashboard, the owner can call the customer
        msg = send_order_confirmation(db, order)
        sent += int(msg is not None and msg.status is MessageStatus.SENT)
    return sent


def send_shipped_notices(db: Session, shop: Shop, now: datetime) -> int:
    """'Order shipped' WhatsApp notices, outside quiet hours."""
    from app.services.order_admin import pending_shipped_notices, send_shipped_notice

    if is_quiet_hours(shop, now):
        return 0
    return sum(send_shipped_notice(db, o) for o in pending_shipped_notices(db, shop))


def awaiting_since(db: Session, order: Order) -> datetime | None:
    return db.scalar(
        select(func.max(Event.created_at)).where(
            Event.order_id == order.id, Event.to_status == S.AWAITING_CONFIRMATION
        )
    )


def process_reminders(db: Session, shop: Shop, now: datetime) -> dict[str, int]:
    """Reminders after shop.reminder_delays hours, then NO_ANSWER after the timeout."""
    settings = get_settings()
    quiet = is_quiet_hours(shop, now, settings)
    delays = sorted(int(h) for h in (shop.reminder_delays or [])) or [3, 24]
    counts = {"reminders": 0, "no_answer": 0}

    orders = db.scalars(
        select(Order).where(Order.shop_id == shop.id, Order.status == S.AWAITING_CONFIRMATION)
    ).all()
    for order in orders:
        since = awaiting_since(db, order)
        if since is None:
            continue
        # The UTCDateTime type returns aware datetimes, but func.max() bypasses it on SQLite.
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        elapsed = now - since

        timeout = timedelta(hours=delays[-1] + settings.no_answer_after_hours)
        if elapsed >= timeout and order.reminders_sent >= len(delays):
            transition(db, order, S.NO_ANSWER, Actor.BOT, note="no answer after reminders")
            counts["no_answer"] += 1
            continue
        if order.customer.opted_out and elapsed >= timeout:
            transition(db, order, S.NO_ANSWER, Actor.BOT, note="customer opted out")
            counts["no_answer"] += 1
            continue

        nxt = order.reminders_sent
        if nxt >= len(delays) or quiet or order.customer.opted_out:
            continue
        if elapsed < timedelta(hours=delays[nxt]):
            continue
        last_in = order.customer.last_inbound_at
        if last_in is not None and last_in > since:
            continue  # the customer is talking with us / the owner: no automatic reminder
        msg = send_order_template(db, order, m.ORDER_REMINDER)
        if msg is not None and msg.status is MessageStatus.SENT:
            order.reminders_sent += 1
            counts["reminders"] += 1
    db.commit()
    return counts


def daily_summary_text(db: Session, shop: Shop, now: datetime) -> str:
    day = local_now(shop, now).date()
    start, end = local_day_bounds(day, shop.timezone)
    st = compute_stats(db, shop, start, end)
    to_check = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.shop_id == shop.id, Order.status == S.MODIFY_REQUESTED)
    )
    return (
        f"📊 {shop.name} – {day.strftime('%d/%m/%Y')}\n"
        f"Commandes : {st.total}\n"
        f"✅ Confirmées : {st.confirmed} ({st.confirmation_rate}%)\n"
        f"❌ Annulées : {st.cancelled}\n"
        f"📵 Sans réponse : {st.no_answer}\n"
        f"⏳ En attente : {st.pending}\n"
        f"✏️ Modifications à traiter : {to_check}\n"
        f"💰 Frais de livraison économisés : {m.format_amount(st.money_saved)} DT"
    )


def daily_jobs(db: Session, shop: Shop, now: datetime) -> bool:
    """Once per local day at DAILY_SUMMARY_HOUR: summary to the owner + retention purge."""
    settings = get_settings()
    local = local_now(shop, now)
    if local.hour < settings.daily_summary_hour or shop.last_daily_run_on == local.date():
        return False
    notify_owner(shop, daily_summary_text(db, shop, now))
    purge_expired(db, shop, now)
    shop.last_daily_run_on = local.date()
    db.commit()
    return True


def tick(now: datetime | None = None) -> None:
    """Run every job for every shop. Errors in one shop never stop the others."""
    from app import db as db_module

    now = now or datetime.now(UTC)
    with db_module.SessionLocal() as db:
        shop_ids = list(db.scalars(select(Shop.id)))
    for shop_id in shop_ids:
        with db_module.SessionLocal() as db:
            shop = db.get(Shop, shop_id)
            if shop is None:
                continue
            try:
                send_pending_confirmations(db, shop, now)
                process_reminders(db, shop, now)
                send_shipped_notices(db, shop, now)
                sync_shop(db, shop)
                send_pending_purchases(db, shop, now)
                billing_jobs(db, shop, now)
                daily_jobs(db, shop, now)
            except Exception:
                log.exception("scheduler: shop jobs failed", extra={"shop_id": shop_id})
                db.rollback()


# ----------------------------------------------------------------- runner
_scheduler = None


def start_scheduler() -> None:
    """Start APScheduler in the background (called from the app lifespan)."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    if _scheduler is not None:
        return
    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        tick,
        "interval",
        seconds=settings.scheduler_interval_seconds,
        id="cod-confirm-tick",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler: started", extra={"interval_s": settings.scheduler_interval_seconds})


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
