"""Server-side chart data + SVG geometry for the dashboard (no JS charting library).

Stacked columns "orders per day by outcome". Mark specs follow the dataviz rules:
columns <= 24px, 4px rounded data-end, square at the baseline, 2px surface gap between
stacked segments, hairline grid, clean y ticks. Colors are categorical slots 1-4 in fixed
order (validated for light + dark; a legend and a table view are always rendered).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import OrderStatus
from app.models import Order, Shop
from app.services.stats import local_day_bounds

S = OrderStatus

# Outcome series, in fixed categorical order (slot 1..4).
SERIES: tuple[tuple[str, str, frozenset[OrderStatus]], ...] = (
    ("confirmed", "Confirmées", frozenset({S.CONFIRMED, S.SHIPPED, S.DELIVERED, S.REFUSED})),
    ("cancelled", "Annulées", frozenset({S.CANCELLED})),
    ("no_answer", "Sans réponse", frozenset({S.NO_ANSWER})),
    ("pending", "En cours", frozenset({S.NEW, S.AWAITING_CONFIRMATION, S.MODIFY_REQUESTED})),
)
DAYS_FR = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")


@dataclass
class DayPoint:
    day: date
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def label(self) -> str:
        return f"{DAYS_FR[self.day.weekday()]} {self.day:%d/%m}"


def daily_outcomes(db: Session, shop: Shop, last_day: date, days: int) -> list[DayPoint]:
    """Orders created per local day over `days` days ending on `last_day` (inclusive)."""
    first_day = last_day - timedelta(days=days - 1)
    start, _ = local_day_bounds(first_day, shop.timezone)
    _, end = local_day_bounds(last_day, shop.timezone)
    zone = ZoneInfo(shop.timezone or "Africa/Tunis")
    points = {
        first_day + timedelta(days=i): DayPoint(
            first_day + timedelta(days=i), {k: 0 for k, *_ in SERIES}
        )
        for i in range(days)
    }
    rows = db.execute(
        select(Order.created_at, Order.status).where(
            Order.shop_id == shop.id, Order.created_at >= start, Order.created_at < end
        )
    )
    for created_at, status in rows:
        day = created_at.astimezone(zone).date()
        if day not in points:
            continue
        for key, _label, statuses in SERIES:
            if status in statuses:
                points[day].counts[key] += 1
                break
    return [points[d] for d in sorted(points)]


# ---------------------------------------------------------------- geometry
@dataclass
class Segment:
    key: str
    value: int
    x: float
    y: float
    w: float
    h: float
    rounded: bool  # top segment of the column: 4px rounded data-end

    @property
    def path(self) -> str:
        """Rect with rounded top corners only (square at the baseline)."""
        x, y, w, h = self.x, self.y, self.w, self.h
        r = min(4.0, w / 2, h) if self.rounded else 0.0
        return (
            f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"
        )


@dataclass
class Column:
    point: DayPoint
    x: float  # band start
    band: float
    segments: list[Segment]
    show_label: bool

    @property
    def tip(self) -> str:
        parts = [self.point.label, f"Total|{self.point.total}"]
        parts += [f"{label}|{self.point.counts[key]}" for key, label, _ in SERIES]
        return ";".join(parts)


@dataclass
class Chart:
    width: int
    height: int
    plot_left: int
    plot_bottom: int
    columns: list[Column]
    ticks: list[tuple[int, float]]  # (value, y)
    series: tuple[tuple[str, str, frozenset[OrderStatus]], ...] = SERIES

    @property
    def empty(self) -> bool:
        return all(c.point.total == 0 for c in self.columns)


def _nice_max(value: int) -> tuple[int, int]:
    """A clean axis maximum and step (0 / 5 / 10..., 0 / 20 / 40...)."""
    if value <= 4:
        return 4, 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if value / step <= 5:
            top = step * -(-value // step)
            return top, step
    step = 10 ** len(str(value))
    return step, step // 5


def build_chart(points: list[DayPoint], width: int = 720, height: int = 240) -> Chart:
    left, right, top, bottom = 36, 8, 12, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    y_max, step = _nice_max(max((p.total for p in points), default=0))
    scale = plot_h / y_max if y_max else 0
    baseline = top + plot_h
    band = plot_w / max(len(points), 1)
    bar_w = min(24.0, band * 0.62)
    every = 1 if len(points) <= 10 else (2 if len(points) <= 16 else 5)

    columns: list[Column] = []
    for i, p in enumerate(points):
        x = left + i * band + (band - bar_w) / 2
        y = float(baseline)
        segs: list[Segment] = []
        keys = [k for k, *_ in SERIES if p.counts.get(k)]
        for key in keys:
            h = p.counts[key] * scale
            seg_y = y - h
            # 2px surface gap between stacked segments, taken from the lower one's top,
            # so every column still starts exactly on the baseline.
            gap = 2.0 if key != keys[-1] else 0.0
            segs.append(Segment(key, p.counts[key], x, seg_y + gap, bar_w,
                                max(h - gap, 0.5), rounded=key == keys[-1]))  # fmt: skip
            y = seg_y
        last = i == len(points) - 1
        columns.append(Column(p, left + i * band, band, segs, show_label=(i % every == 0) or last))
    ticks = [(v, baseline - v * scale) for v in range(0, y_max + 1, step)]
    return Chart(width, height, left, baseline, columns, ticks)


@dataclass(frozen=True)
class ShareBlock:
    key: str
    label: str
    count: int
    pct: float
    x: float
    w: float


def share_blocks(
    counts: dict[str, int], width: float = 1000.0, gap: float = 4.0
) -> list[ShareBlock]:
    """One whole split into parts (outcome shares), blocks separated by a surface gap."""
    total = sum(counts.get(k, 0) for k, *_ in SERIES)
    present = [k for k, *_ in SERIES if counts.get(k, 0) > 0]
    usable = width - gap * max(len(present) - 1, 0)
    blocks, x = [], 0.0
    for key, label, _ in SERIES:
        n = counts.get(key, 0)
        w = usable * n / total if total else 0.0
        blocks.append(ShareBlock(key, label, n, round(100 * n / total, 1) if total else 0.0, x, w))
        if n:
            x += w + gap
    return blocks
