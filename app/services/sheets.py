"""Google Sheets sync (optional, per shop).

Each order is one row of the shop's sheet (tab "Commandes"), created on the first sync and
updated on every change. Changes are flagged on the order (`sheet_dirty`) and pushed by the
scheduler in batches, so a slow or failing Google API never slows down WhatsApp.

Setup: one Google service account for the SaaS (GOOGLE_SERVICE_ACCOUNT_FILE). The seller
shares their sheet with the service-account email (Editor) and pastes the sheet id in
the settings. Values are written RAW: a customer name like "=HYPERLINK(...)" is never
evaluated as a formula.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Order, Shop
from app.services import messages as m

log = logging.getLogger(__name__)

WORKSHEET = "Commandes"
HEADER = ["ID", "Date", "Statut", "Client", "Téléphone", "Ville", "Adresse", "Produits",
          "Total", "Devise", "Source", "Relances", "Mis à jour"]  # fmt: skip
BATCH_SIZE = 50
STATUS_FR = {
    "NEW": "Nouvelle", "AWAITING_CONFIRMATION": "En attente", "CONFIRMED": "Confirmée",
    "MODIFY_REQUESTED": "À modifier", "CANCELLED": "Annulée", "NO_ANSWER": "Sans réponse",
    "SHIPPED": "Expédiée", "DELIVERED": "Livrée", "REFUSED": "Refusée",
}  # fmt: skip


class SheetBackend(Protocol):
    def order_rows(self) -> dict[str, int]:
        """Map order id (column A) -> 1-based row number."""
        ...

    def update_rows(self, updates: Sequence[tuple[int, list[str]]]) -> None: ...

    def append_rows(self, rows: Sequence[list[str]]) -> None: ...


class GspreadBackend:
    """Real backend (gspread). 3 API calls per sync at most: read column A, update, append."""

    def __init__(self, sheet_id: str, service_account_file: str) -> None:
        import gspread

        client = gspread.service_account(filename=service_account_file)
        spreadsheet = client.open_by_key(sheet_id)
        try:
            self.ws = spreadsheet.worksheet(WORKSHEET)
        except gspread.WorksheetNotFound:
            self.ws = spreadsheet.add_worksheet(WORKSHEET, rows=1000, cols=len(HEADER))
            self.ws.append_row(HEADER, value_input_option="RAW")

    def order_rows(self) -> dict[str, int]:
        return {v: i for i, v in enumerate(self.ws.col_values(1), start=1) if v and v != "ID"}

    def update_rows(self, updates: Sequence[tuple[int, list[str]]]) -> None:
        if updates:
            last_col = chr(ord("A") + len(HEADER) - 1)
            self.ws.batch_update(
                [
                    {"range": f"A{row}:{last_col}{row}", "values": [values]}
                    for row, values in updates
                ],
                value_input_option="RAW",
            )

    def append_rows(self, rows: Sequence[list[str]]) -> None:
        if rows:
            self.ws.append_rows(list(rows), value_input_option="RAW")


def order_row(order: Order, tz: str) -> list[str]:
    zone = ZoneInfo(tz or "Africa/Tunis")
    local = lambda d: d.astimezone(zone).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    return [
        str(order.id),
        local(order.created_at),
        STATUS_FR.get(order.status.value, order.status.value),
        order.customer.name,
        order.customer.phone_e164,
        order.city,
        order.address,
        m.items_summary(order.items, 500),
        m.format_amount(order.total),
        order.currency,
        order.source.value,
        str(order.reminders_sent),
        local(order.updated_at),
    ]


def service_account_email() -> str | None:
    path = get_settings().google_service_account_file
    if not path or not Path(path).is_file():
        return None
    try:
        return str(json.loads(Path(path).read_text(encoding="utf-8")).get("client_email") or "")
    except (OSError, ValueError):
        return None


def backend_for_shop(shop: Shop) -> SheetBackend | None:
    path = get_settings().google_service_account_file
    if not shop.google_sheet_id or not path:
        return None
    return GspreadBackend(shop.google_sheet_id, path)


def sync_shop(db: Session, shop: Shop, backend: SheetBackend | None = None) -> int:
    """Push up to BATCH_SIZE changed orders. Returns the number of rows written."""
    if not shop.google_sheet_id:
        return 0
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.shop_id == shop.id, Order.sheet_dirty.is_(True))
            .order_by(Order.id)
            .limit(BATCH_SIZE)
        )
    )
    if not orders:
        return 0
    try:
        backend = backend or backend_for_shop(shop)
        if backend is None:
            return 0
        existing = backend.order_rows()
        updates, appends = [], []
        for order in orders:
            row = order_row(order, shop.timezone)
            if str(order.id) in existing:
                updates.append((existing[str(order.id)], row))
            else:
                appends.append(row)
        backend.update_rows(updates)
        backend.append_rows(appends)
    except Exception:  # Google errors: keep the flags, retry at the next tick
        log.exception("sheets: sync failed", extra={"shop_id": shop.id})
        return 0

    for order in orders:
        order.sheet_dirty = False
    db.commit()
    log.info("sheets: synced", extra={"shop_id": shop.id, "rows": len(orders),
                                      "at": datetime.now().isoformat()})  # fmt: skip
    return len(orders)
