"""Delivery: hand confirmed orders to a carrier and bring delivery results back.

Tunisian carriers (First Delivery, Intigo, Navex, Rapid-Poste...) mostly work with
spreadsheets and private APIs given to contracted merchants. This module therefore works
with ANY carrier today:

* ship one or many orders (carrier + tracking number) -> SHIPPED + WhatsApp notice;
* export a carrier import file (CSV) and print a manifest / parcel labels;
* import the status file the carrier sends back (delivered / returned) -> DELIVERED / REFUSED;
* receive signed status updates on /webhooks/carrier/<slug> (any carrier or integrator).

A carrier-specific API adapter can be added later on top of `apply_carrier_status`.
"""

from __future__ import annotations

import csv
import io
import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Actor, OrderStatus
from app.models import Order, Shop
from app.services import messages as m
from app.services.orders import InvalidTransition, transition

log = logging.getLogger(__name__)
S = OrderStatus

CARRIERS: dict[str, str] = {
    "first_delivery": "First Delivery",
    "intigo": "Intigo",
    "navex": "Navex",
    "aramex": "Aramex",
    "rapid_poste": "Rapid-Poste",
    "own": "Livreur de la boutique",
    "other": "Autre",
}
MAX_IMPORT_BYTES = 2_000_000
MAX_IMPORT_ROWS = 5_000


def carrier_label(code: str | None) -> str:
    return CARRIERS.get(code or "", code or "—")


def tracking_url(shop: Shop, order: Order) -> str | None:
    """Public tracking link, from the shop's pattern like 'https://x.tn/suivi?code={tracking}'."""
    if not order.tracking_number or not shop.tracking_url_pattern:
        return None
    pattern = shop.tracking_url_pattern
    if not pattern.startswith("https://") or "{tracking}" not in pattern:
        return None
    return pattern.replace("{tracking}", quote(order.tracking_number, safe=""))


# ------------------------------------------------------------------ shipping
def ship_order(
    db: Session, order: Order, carrier: str, tracking_number: str | None, by: str
) -> None:
    """CONFIRMED -> SHIPPED with carrier info. Commit is left to the caller."""
    if carrier not in CARRIERS:
        raise ValueError("unknown carrier")
    tracking = (tracking_number or "").strip()[:64] or None
    transition(db, order, S.SHIPPED, Actor.ADMIN, note=f"{carrier_label(carrier)} · {by}"[:300])
    order.carrier = carrier
    order.tracking_number = tracking
    order.shipped_at = datetime.now(UTC)
    order.shipped_notified_at = None  # the "order shipped" WhatsApp notice is still to send


def ship_many(
    db: Session, shop: Shop, order_ids: Iterable[int], carrier: str, by: str
) -> list[Order]:
    orders = db.scalars(
        select(Order).where(Order.shop_id == shop.id, Order.id.in_(list(order_ids)[:500]),
                            Order.status == S.CONFIRMED)
    ).all()  # fmt: skip
    for order in orders:
        ship_order(db, order, carrier, None, by)
    db.commit()
    return list(orders)


def ready_to_ship(db: Session, shop: Shop, limit: int = 300) -> list[Order]:
    return list(db.scalars(
        select(Order).where(Order.shop_id == shop.id, Order.status == S.CONFIRMED)
        .order_by(Order.id).limit(limit)
    ))  # fmt: skip


def in_transit(db: Session, shop: Shop, limit: int = 300) -> list[Order]:
    return list(db.scalars(
        select(Order).where(Order.shop_id == shop.id, Order.status == S.SHIPPED)
        .order_by(Order.shipped_at.desc(), Order.id.desc()).limit(limit)
    ))  # fmt: skip


def selected_orders(db: Session, shop: Shop, ids: Sequence[int]) -> list[Order]:
    if not ids:
        return ready_to_ship(db, shop)
    return list(db.scalars(
        select(Order)
        .where(Order.shop_id == shop.id, Order.id.in_(list(ids)[:500]))
        .order_by(Order.id)
    ))  # fmt: skip


# -------------------------------------------------------------------- export
EXPORT_HEADER = ["Référence", "Nom", "Téléphone", "Gouvernorat", "Adresse", "Produits",
                 "Quantité", "Montant COD (DT)", "Remarque"]  # fmt: skip
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def carrier_export_csv(orders: Sequence[Order]) -> str:
    """Generic import file accepted (after column mapping) by Tunisian carrier platforms."""
    buf = io.StringIO()
    buf.write("﻿")  # Excel: UTF-8 BOM
    w = csv.writer(buf, delimiter=";")
    w.writerow(EXPORT_HEADER)
    for o in orders:
        qty = sum(int(i.get("qty", 1)) for i in o.items)
        w.writerow([_safe(v) for v in (
            f"CC-{o.id}", o.customer.name, o.customer.phone_e164.replace("+216", ""), o.city,
            o.address, m.items_summary(o.items, 300), qty, m.format_amount(o.total),
            "Paiement à la livraison",
        )])  # fmt: skip
    return buf.getvalue()


# --------------------------------------------------------- status from carrier
def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.strip().lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def map_carrier_status(raw: str) -> OrderStatus | None:
    """'Livré' -> DELIVERED, 'Retour' / 'Refusé' -> REFUSED, in-transit states -> None."""
    s = _norm(raw)
    if not s:
        return None
    if any(k in s for k in ("retour", "refus", "return", "rejected", "annul", "cancel")):
        return S.REFUSED
    if "cours" in s or "en livraison" in s or "transit" in s or "out for" in s:
        return None
    if s.startswith(("livr", "deliver")) or s in ("ok", "paye", "encaisse", "delivered"):
        return S.DELIVERED
    return None


def find_order(db: Session, shop: Shop, ref: str) -> Order | None:
    """Accepts our reference 'CC-123', a plain order number or a tracking number."""
    ref = ref.strip()
    if not ref or len(ref) > 64:
        return None
    by_tracking = db.scalar(
        select(Order).where(Order.shop_id == shop.id, Order.tracking_number == ref)
    )
    if by_tracking is not None:
        return by_tracking
    digits = ref.upper().removeprefix("CC-").strip()
    if digits.isdigit():
        return db.scalar(select(Order).where(Order.shop_id == shop.id, Order.id == int(digits)))
    return None


def apply_carrier_status(db: Session, order: Order, status: OrderStatus, source: str) -> bool:
    """Apply DELIVERED / REFUSED to a shipped order. Returns True when it changed."""
    if status not in (S.DELIVERED, S.REFUSED) or order.status is not S.SHIPPED:
        return False
    try:
        transition(db, order, status, Actor.ADMIN, note=f"transporteur · {source}"[:300])
    except InvalidTransition:
        return False
    return True


@dataclass
class ImportReport:
    delivered: int = 0
    refused: int = 0
    unchanged: int = 0
    not_found: list[str] = field(default_factory=list)
    error: str | None = None


_ORDER_COLS = ("reference", "commande", "order", "ref", "numero commande", "id")
_TRACK_COLS = ("suivi", "tracking", "code", "barcode", "awb", "colis")
_STATUS_COLS = ("statut", "status", "etat")


def _pick(header: list[str], keys: Sequence[str]) -> int | None:
    for i, h in enumerate(header):
        if any(k in h for k in keys):
            return i
    return None


def import_status_csv(db: Session, shop: Shop, data: bytes) -> ImportReport:
    """Read the carrier's status file (CSV ; or ,) and update shipped orders."""
    report = ImportReport()
    if len(data) > MAX_IMPORT_BYTES:
        report.error = "Fichier trop volumineux (2 Mo maximum)."
        return report
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) < 2:
        report.error = "Fichier vide."
        return report
    header = [_norm(h) for h in rows[0]]
    status_i = _pick(header, _STATUS_COLS)
    track_i, order_i = _pick(header, _TRACK_COLS), _pick(header, _ORDER_COLS)
    if status_i is None or (track_i is None and order_i is None):
        report.error = "Colonnes introuvables : il faut « Statut » et « Suivi » ou « Référence »."
        return report

    for row in rows[1 : MAX_IMPORT_ROWS + 1]:
        if len(row) <= status_i:
            continue
        refs = [
            row[i] for i in (track_i, order_i) if i is not None and i < len(row) and row[i].strip()
        ]
        order = next((o for o in (find_order(db, shop, r) for r in refs) if o is not None), None)
        if order is None:
            if refs:
                report.not_found.append(refs[0][:40])
            continue
        status = map_carrier_status(row[status_i])
        if status is not None and apply_carrier_status(db, order, status, "import"):
            report.delivered += status is S.DELIVERED
            report.refused += status is S.REFUSED
        else:
            report.unchanged += 1
    db.commit()
    log.info("shipping: status import", extra={"shop_id": shop.id, "delivered": report.delivered,
                                               "refused": report.refused})  # fmt: skip
    return report
