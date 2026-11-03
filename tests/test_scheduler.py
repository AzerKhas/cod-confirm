from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import Direction, MessageStatus, OrderStatus
from app.models import Order, Shop
from app.services import scheduler as sch
from app.services.notify import send_order_confirmation
from tests.factories import make_customer, make_order, make_shop

S = OrderStatus
TUNIS_NOON_UTC = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)  # 12:00 in Tunis (UTC+1)


@pytest.fixture(autouse=True)
def quiet_hours_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("QUIET_HOURS_START", "22")
    monkeypatch.setenv("QUIET_HOURS_END", "8")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(sch, "notify_owner", lambda shop, text, **_: sent.append(text) or True)
    return sent


def _at_local(hour: int, minute: int = 0) -> datetime:
    """A UTC datetime that is `hour:minute` in Tunis (no DST, UTC+1)."""
    return datetime(2026, 11, 4, hour, minute, tzinfo=UTC) - timedelta(hours=1)


# ------------------------------------------------------------- quiet hours
@pytest.mark.parametrize(
    ("hour", "quiet"),
    [(21, False), (22, True), (23, True), (0, True), (3, True), (7, True), (8, False), (12, False)],
)
def test_quiet_hours_in_tunis_time(hour: int, quiet: bool) -> None:
    shop = Shop(timezone="Africa/Tunis")
    assert sch.is_quiet_hours(shop, _at_local(hour, 30 if hour == 7 else 0)) is quiet


def test_quiet_hours_follow_shop_timezone() -> None:
    utc_23 = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
    assert sch.is_quiet_hours(Shop(timezone="Africa/Tunis"), utc_23)  # 00:00 Tunis
    assert not sch.is_quiet_hours(Shop(timezone="America/New_York"), utc_23)  # 18:00 NY


# ----------------------------------------------------- pending confirmations
def test_night_order_waits_for_morning(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    db_session.commit()

    assert sch.send_pending_confirmations(db_session, shop, _at_local(23)) == 0
    assert order.status is S.NEW
    assert sch.send_pending_confirmations(db_session, shop, _at_local(8, 1)) == 1
    assert order.status is S.AWAITING_CONFIRMATION


def test_background_job_respects_quiet_hours(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import notify

    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    db_session.commit()
    monkeypatch.setattr(sch, "is_quiet_hours", lambda *_a, **_k: True)
    notify.send_order_confirmation_job(order.id)
    db_session.expire_all()
    assert order.status is S.NEW


def test_failed_sends_stop_after_max(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import notify
    from app.services.whatsapp import WhatsAppError

    class Broken:
        def send_template(self, *a: Any, **k: Any) -> None:
            raise WhatsAppError("down")

    monkeypatch.setattr(notify, "client_for_shop", lambda shop: Broken())
    shop = make_shop(db_session)
    order = make_order(db_session, shop)
    db_session.commit()
    for _ in range(5):
        sch.send_pending_confirmations(db_session, shop, TUNIS_NOON_UTC)
    failed = [x for x in order.messages if x.status is MessageStatus.FAILED]
    assert len(failed) == 3  # MAX_SEND_FAILURES
    assert order.status is S.NEW


def test_opted_out_skipped(db_session: Session) -> None:
    shop = make_shop(db_session)
    make_order(db_session, shop, make_customer(db_session, shop, opted_out=True))
    db_session.commit()
    assert sch.send_pending_confirmations(db_session, shop, TUNIS_NOON_UTC) == 0


# ----------------------------------------------------------------- reminders
def _awaiting(db: Session, shop: Shop, **kw: Any) -> tuple[Order, datetime]:
    order = make_order(db, shop, **kw)
    send_order_confirmation(db, order)
    assert order.status is S.AWAITING_CONFIRMATION
    since = sch.awaiting_since(db, order)
    assert since is not None
    return order, since if since.tzinfo else since.replace(tzinfo=UTC)


def _first_daytime_after(t: datetime) -> datetime:
    """Move `t` out of quiet hours if needed (keeps tests independent of the real clock)."""
    shop = Shop(timezone="Africa/Tunis")
    while sch.is_quiet_hours(shop, t):
        t += timedelta(hours=1)
    return t


def test_reminders_then_no_answer(db_session: Session) -> None:
    shop = make_shop(db_session, reminder_delays=[3, 24])
    order, since = _awaiting(db_session, shop)

    assert sch.process_reminders(db_session, shop, since + timedelta(hours=1)) == {
        "reminders": 0,
        "no_answer": 0,
    }

    t1 = _first_daytime_after(since + timedelta(hours=3, minutes=1))
    assert sch.process_reminders(db_session, shop, t1)["reminders"] == 1
    assert order.reminders_sent == 1
    assert sch.process_reminders(db_session, shop, t1)["reminders"] == 0  # idempotent

    t2 = _first_daytime_after(since + timedelta(hours=24, minutes=1))
    assert sch.process_reminders(db_session, shop, t2)["reminders"] == 1
    assert order.reminders_sent == 2

    templates = [x for x in order.messages if x.direction is Direction.OUT]
    assert len(templates) == 3  # confirmation + 2 reminders
    assert "Mazelna nestannew jawbek" in templates[-1].content

    t3 = since + timedelta(hours=24 + 24, minutes=1)
    assert sch.process_reminders(db_session, shop, t3)["no_answer"] == 1
    assert order.status is S.NO_ANSWER
    assert order.events[-1].actor.value == "bot"


def test_no_reminder_during_quiet_hours(db_session: Session) -> None:
    shop = make_shop(db_session)
    order, since = _awaiting(db_session, shop)
    t = since + timedelta(hours=3, minutes=1)
    night = t.replace(hour=22, minute=30) - timedelta(hours=1)  # 22:30 Tunis
    if night < t:
        night += timedelta(days=1)
    assert sch.process_reminders(db_session, shop, night)["reminders"] == 0
    assert order.reminders_sent == 0


def test_no_reminder_when_customer_is_talking(db_session: Session) -> None:
    shop = make_shop(db_session)
    order, since = _awaiting(db_session, shop)
    order.customer.last_inbound_at = since + timedelta(minutes=10)
    t = _first_daytime_after(since + timedelta(hours=4))
    assert sch.process_reminders(db_session, shop, t)["reminders"] == 0


def test_opted_out_goes_to_no_answer_without_reminders(db_session: Session) -> None:
    shop = make_shop(db_session)
    order, since = _awaiting(db_session, shop)
    order.customer.opted_out = True
    assert sch.process_reminders(db_session, shop, since + timedelta(hours=5))["reminders"] == 0
    assert sch.process_reminders(db_session, shop, since + timedelta(hours=49))["no_answer"] == 1
    assert order.status is S.NO_ANSWER


# -------------------------------------------------------------- daily summary
def test_daily_summary_once_per_day(db_session: Session, alerts: list[str]) -> None:
    shop = make_shop(db_session, name="Boutique Yasmine", delivery_fee=Decimal("8"))
    now = datetime.now(UTC)
    make_order(db_session, shop, status=S.CONFIRMED, created_at=now)
    make_order(db_session, shop, status=S.CANCELLED, created_at=now)
    make_order(db_session, shop, status=S.NO_ANSWER, created_at=now)
    db_session.commit()

    local_date = sch.local_now(shop, now).date()
    evening = datetime(
        local_date.year, local_date.month, local_date.day, 20, 30, tzinfo=UTC
    )  # 21:30 Tunis
    morning = evening - timedelta(hours=12)

    assert sch.daily_jobs(db_session, shop, morning) is False
    assert sch.daily_jobs(db_session, shop, evening) is True
    assert sch.daily_jobs(db_session, shop, evening + timedelta(minutes=30)) is False  # once
    assert len(alerts) == 1
    text = alerts[0]
    assert "Boutique Yasmine" in text
    assert "Confirmées : 1" in text and "Annulées : 1" in text and "Sans réponse : 1" in text
    assert "16 DT" in text  # 2 orders stopped before shipping x 8 DT


def test_tick_runs_all_shops(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    s1, s2 = make_shop(db_session), make_shop(db_session)
    o1, o2 = make_order(db_session, s1), make_order(db_session, s2)
    db_session.commit()
    sch.tick(TUNIS_NOON_UTC)
    db_session.expire_all()
    assert o1.status is S.AWAITING_CONFIRMATION and o2.status is S.AWAITING_CONFIRMATION


def test_tick_isolates_shop_errors(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    s1, s2 = make_shop(db_session), make_shop(db_session)
    o2 = make_order(db_session, s2)
    db_session.commit()
    real = sch.send_pending_confirmations

    def flaky(db: Session, shop: Shop, now: datetime) -> int:
        if shop.id == s1.id:
            raise RuntimeError("boom")
        return real(db, shop, now)

    monkeypatch.setattr(sch, "send_pending_confirmations", flaky)
    sch.tick(TUNIS_NOON_UTC)
    db_session.expire_all()
    assert o2.status is S.AWAITING_CONFIRMATION
