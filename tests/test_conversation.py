from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Direction, Language, OrderStatus
from app.models import Customer, MessageLog, Order, Shop
from app.services import conversation as conv
from app.services.inbound import WaMessage, store_message
from app.services.intent import Intent
from app.services.whatsapp import ReplyButton, SendResult, WhatsAppError
from tests.factories import make_customer, make_order, make_shop

S = OrderStatus
_ids = count(1)


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.buttons: list[tuple[str, list[ReplyButton]]] = []
        self.read: list[str] = []
        self.fail = fail

    def send_text(self, to: str, body: str) -> SendResult:
        if self.fail:
            raise WhatsAppError("boom", code=131047)
        self.texts.append(body)
        return SendResult(f"wamid.out{next(_ids)}")

    def send_buttons(self, to: str, body: str, buttons: list[ReplyButton]) -> SendResult:
        self.buttons.append((body, list(buttons)))
        return SendResult(f"wamid.out{next(_ids)}")

    def mark_as_read(self, message_id: str) -> None:
        self.read.append(message_id)


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(conv, "notify_owner", lambda shop, text, **_: sent.append(text) or True)
    return sent


@pytest.fixture
def world(db_session: Session) -> tuple[Shop, Customer, Order]:
    shop = make_shop(db_session, name="Boutique Yasmine", whatsapp_phone_number_id="PN1")
    customer = make_customer(db_session, shop, name="Ahmed Ben Salah", phone_e164="+21698123456")
    order = make_order(db_session, shop, customer, status=S.AWAITING_CONFIRMATION)
    db_session.commit()
    return shop, customer, order


def say(db: Session, shop: Shop, text: str = "", payload: str | None = None,
        client: FakeClient | None = None, classifier: Any = None) -> tuple[Intent, FakeClient]:  # fmt: skip
    client = client or FakeClient()
    raw: dict[str, Any] = {"from": "21698123456", "id": f"wamid.in{next(_ids)}", "type": "text",
                           "text": {"body": text}}  # fmt: skip
    if payload:
        raw = {"from": "21698123456", "id": f"wamid.in{next(_ids)}", "type": "button",
               "button": {"payload": payload, "text": text}}  # fmt: skip
    msg = store_message(db, shop, WaMessage.model_validate(raw))
    assert msg is not None
    intent = conv.handle_incoming(db, shop, msg, client=client, classifier=classifier)  # type: ignore[arg-type]
    db.commit()
    return intent, client


# ----------------------------------------------------------------- main flows
def test_confirm_by_text(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    intent, client = say(db_session, shop, "ey nconfirmi")
    assert intent is Intent.CONFIRM
    assert order.status is S.CONFIRMED
    assert client.texts == ["Ya3tik essa7a Ahmed ✅ Commande mte3ek tconfirmet. "
                            "Bech nab3thouhalek fi a9rab wa9t 🚚"]  # fmt: skip
    assert len(client.read) == 1  # blue ticks
    assert order.events[-1].actor.value == "customer"
    assert alerts == []


def test_confirm_by_button(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    say(db_session, shop, "✅ Nconfirmi", payload=f"CONFIRM:{order.id}")
    assert order.status is S.CONFIRMED


def test_confirm_twice(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    say(db_session, shop, "ey")
    _i, client = say(db_session, shop, "ok")
    assert order.status is S.CONFIRMED
    assert "deja mconfirmia" in client.texts[0]


def test_cancel(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    _i, client = say(db_session, shop, "la ma n7ebch")
    assert order.status is S.CANCELLED
    assert "tannulet" in client.texts[0]
    assert len(alerts) == 1 and "annulée" in alerts[0]


def test_cancel_after_shipping_is_refused(
    db_session: Session, world: Any, alerts: list[str]
) -> None:
    shop, _c, order = world
    order.status = S.SHIPPED
    db_session.commit()
    _i, client = say(db_session, shop, "nannuli")
    assert order.status is S.SHIPPED
    assert "5arjet m3a el livreur" in client.texts[0]
    assert "expédiée" in alerts[0]


def test_modify_flow(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    _i, client = say(db_session, shop, "n7eb nbaddel l adresse")
    assert order.status is S.MODIFY_REQUESTED
    assert "chnowa t7eb tbaddel" in client.texts[0]
    assert "modification" in alerts[0]

    # The customer then writes the change in free text -> ack once + owner alerted.
    _i, client2 = say(db_session, shop, "Route de Tunis km 5, Sfax")
    _i, client3 = say(db_session, shop, "9rib l pharmacie")
    assert "Wselna" in client2.texts[0]
    assert client3.texts == []  # no repeated ack
    assert len(alerts) == 3
    assert "Route de Tunis km 5" in alerts[1]


def test_question_forwarded(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, _c, order = world
    _i, client = say(db_session, shop, "9adech twalli l livraison?")
    assert order.status is S.AWAITING_CONFIRMATION
    assert "Sou2alek wsel" in client.texts[0]
    assert "question" in alerts[0]
    assert "+21698123456" in alerts[0]  # the owner needs the number to answer


def test_unknown_clarifies_with_buttons_then_stops(db_session: Session, world: Any,
                                                    alerts: list[str]) -> None:  # fmt: skip
    shop, _c, order = world
    _i, c1 = say(db_session, shop, "salam")
    _i, c2 = say(db_session, shop, "hmm")
    _i, c3 = say(db_session, shop, "ahla")
    body, buttons = c1.buttons[0]
    assert "ma fhemtekch" in body
    assert [b.id for b in buttons] == [
        f"CONFIRM:{order.id}",
        f"MODIFY:{order.id}",
        f"CANCEL:{order.id}",
    ]
    assert len(c2.buttons) == 1
    assert c3.buttons == [] and c3.texts == []  # max 2 clarifications, then the owner
    assert alerts and "non comprise" in alerts[-1]


def test_llm_fallback(db_session: Session, world: Any, alerts: list[str]) -> None:
    class LLM:
        def classify(self, text: str) -> Intent:
            return Intent.CONFIRM

    shop, _c, order = world
    say(db_session, shop, "inchallah ab3athhouli", classifier=LLM())
    assert order.status is S.CONFIRMED


# ------------------------------------------------------------------- opt-out
def test_stop_opts_out_forever(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, customer, order = world
    _i, client = say(db_session, shop, "ma t3awedch tab3athli")
    assert customer.opted_out is True and customer.opted_out_at is not None
    assert "ma3adech nab3thoulek" in client.texts[0]

    _i, client2 = say(db_session, shop, "ey")  # opted-out: stored, no auto reply, no change
    assert client2.texts == [] and client2.buttons == []
    assert order.status is S.AWAITING_CONFIRMATION

    _i, client3 = say(db_session, shop, "STOP")  # no second acknowledgment
    assert client3.texts == []


def test_start_opts_back_in(db_session: Session, world: Any, alerts: list[str]) -> None:
    shop, customer, _o = world
    say(db_session, shop, "توقف")
    _i, client = say(db_session, shop, "START")
    assert customer.opted_out is False
    assert client.texts[0].startswith("مرحبا")  # customer wrote in Arabic -> Arabic replies


def test_opted_out_customer_gets_no_template(db_session: Session, world: Any) -> None:
    from app.services.notify import send_order_confirmation

    shop, customer, _o = world
    customer.opted_out = True
    new_order = make_order(db_session, shop, customer)
    assert send_order_confirmation(db_session, new_order) is None


# ---------------------------------------------------------- window / language
def test_no_reply_outside_24h_window(db_session: Session, world: Any) -> None:
    shop, customer, order = world
    customer.last_inbound_at = datetime.now(UTC) - timedelta(hours=25)
    convo = conv.Conversation(db_session, shop, MessageLog(order=order, customer_id=customer.id,
                              direction=Direction.IN, content="x"), client=FakeClient())  # type: ignore[arg-type]  # fmt: skip
    convo.reply("confirmed")
    assert convo.client.texts == []  # type: ignore[attr-defined]
    assert conv.in_service_window(customer) is False
    customer.last_inbound_at = datetime.now(UTC) - timedelta(hours=23)
    assert conv.in_service_window(customer) is True


def test_send_failure_is_logged_not_raised(
    db_session: Session, world: Any, alerts: list[str]
) -> None:
    shop, _c, order = world
    say(db_session, shop, "ey", client=FakeClient(fail=True))
    assert order.status is S.CONFIRMED  # the decision is kept even if the reply failed
    failed = db_session.scalars(select(MessageLog).where(MessageLog.error.is_not(None))).all()
    assert failed and failed[0].error == "boom"


def test_arabic_customer_gets_arabic_replies(
    db_session: Session, world: Any, alerts: list[str]
) -> None:
    shop, customer, _o = world
    _i, client = say(db_session, shop, "نعم أكيد")
    assert customer.language is Language.ARABIC
    assert client.texts[0].startswith("شكرا")


def test_french_shop_default(db_session: Session, alerts: list[str]) -> None:
    shop = make_shop(db_session, default_language=Language.FRENCH, whatsapp_phone_number_id="PN2")
    make_order(db_session, shop, make_customer(db_session, shop, phone_e164="+21698123456"),
               status=S.AWAITING_CONFIRMATION)  # fmt: skip
    db_session.commit()
    _i, client = say(db_session, shop, "oui")
    assert client.texts[0].startswith("Merci")


def test_no_open_order(db_session: Session, alerts: list[str]) -> None:
    shop = make_shop(db_session, whatsapp_phone_number_id="PN3")
    make_order(db_session, shop, make_customer(db_session, shop, phone_e164="+21698123456"),
               status=S.DELIVERED)  # fmt: skip
    db_session.commit()
    _i, client = say(db_session, shop, "salam")
    assert "Ma l9inech commande" in client.texts[0]
