from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.enums import OrderStatus
from app.services.charts import DayPoint, build_chart, daily_outcomes, share_blocks
from tests.factories import make_order, make_shop

S = OrderStatus


def _point(d: date, **counts: int) -> DayPoint:
    base = {"confirmed": 0, "cancelled": 0, "no_answer": 0, "pending": 0}
    base.update(counts)
    return DayPoint(d, base)


def test_daily_outcomes_groups_by_local_day_and_outcome(db_session: Session) -> None:
    shop = make_shop(db_session)
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)
    make_order(db_session, shop, status=S.DELIVERED, created_at=now)
    make_order(db_session, shop, status=S.CANCELLED, created_at=now)
    make_order(db_session, shop, status=S.AWAITING_CONFIRMATION, created_at=now - timedelta(days=2))
    make_order(db_session, shop, status=S.CONFIRMED, created_at=now - timedelta(days=40))  # outside
    points = daily_outcomes(db_session, shop, today + timedelta(days=1), 7)
    assert len(points) == 7
    assert sum(p.total for p in points) == 3
    assert sum(p.counts["confirmed"] for p in points) == 1
    assert sum(p.counts["pending"] for p in points) == 1


def test_build_chart_geometry() -> None:
    days = [
        _point(date(2026, 11, 1) + timedelta(days=i), confirmed=i, cancelled=1) for i in range(14)
    ]
    chart = build_chart(days)
    assert chart.ticks[0][0] == 0 and chart.ticks[-1][0] >= 14  # clean axis covering the max
    for col in chart.columns:
        assert all(s.w <= 24 for s in col.segments)  # bar width cap
        if col.segments:
            assert (
                abs(col.segments[0].y + col.segments[0].h - chart.plot_bottom) < 0.01
            )  # on the baseline
            assert [s.rounded for s in col.segments][-1] is True  # only the data-end is rounded
            assert sum(s.rounded for s in col.segments) == 1
    stacked = chart.columns[5].segments
    assert stacked[1].y + stacked[1].h <= stacked[0].y - 1.99  # 2px surface gap between segments
    assert "Confirmées|5" in chart.columns[5].tip
    assert any(not c.show_label for c in chart.columns)  # thinned x labels on 14 days


def test_empty_chart() -> None:
    chart = build_chart([_point(date(2026, 11, 1))])
    assert chart.empty


def test_share_blocks() -> None:
    blocks = share_blocks({"confirmed": 6, "cancelled": 2, "no_answer": 0, "pending": 2})
    assert [b.pct for b in blocks] == [60.0, 20.0, 0.0, 20.0]
    assert blocks[2].w == 0
    assert abs(blocks[-1].x + blocks[-1].w - 1000) < 0.01  # fills the whole bar
    assert share_blocks({})[0].pct == 0.0
