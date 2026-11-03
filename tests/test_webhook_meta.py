from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import Direction, MessageStatus, MessageType, OrderStatus
from app.models import MessageLog
from tests.factories import make_customer, make_order, make_shop

APP_SECRET = "meta-app-secret"
VERIFY = "my-verify-token"
PNID = "106540352242922"


@pytest.fixture
def meta_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("META_VERIFY_TOKEN", VERIFY)
    get_settings.cache_clear()
    yield client
    get_settings.cache_clear()


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client: TestClient, payload: dict[str, Any], secret: str = APP_SECRET) -> Any:
    raw = json.dumps(payload).encode()
    return client.post("/webhook/meta", content=raw, headers={
        "X-Hub-Signature-256": sign(raw, secret), "Content-Type": "application/json"})  # fmt: skip


def envelope(messages: list[dict[str, Any]] | None = None,
             statuses: list[dict[str, Any]] | None = None, pnid: str = PNID) -> dict[str, Any]:  # fmt: skip
    value: dict[str, Any] = {"messaging_product": "whatsapp",
                             "metadata": {"display_phone_number": "21650000000", "phone_number_id": pnid}}  # fmt: skip
    if messages:
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}  # fmt: skip


def text_msg(wamid: str, body: str, sender: str = "21698123456") -> dict[str, Any]:
    return {
        "from": sender,
        "id": wamid,
        "timestamp": "1730000000",
        "type": "text",
        "text": {"body": body},
    }


@pytest.fixture
def setup(db_session: Session) -> Any:
    shop = make_shop(db_session, whatsapp_phone_number_id=PNID)
    customer = make_customer(db_session, shop, phone_e164="+21698123456")
    order = make_order(db_session, shop, customer, status=OrderStatus.AWAITING_CONFIRMATION)
    db_session.commit()
    return shop, customer, order


# --------------------------------------------------------------- verification
def test_verify_ok(meta_client: TestClient) -> None:
    r = meta_client.get("/webhook/meta", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                                 "hub.challenge": "1158201444"})  # fmt: skip
    assert r.status_code == 200
    assert r.text == "1158201444"


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
    {},
])  # fmt: skip
def test_verify_rejected(meta_client: TestClient, params: dict[str, str]) -> None:
    assert meta_client.get("/webhook/meta", params=params).status_code == 403


def test_verify_rejected_when_token_not_configured(client: TestClient) -> None:
    r = client.get("/webhook/meta", params={"hub.mode": "subscribe", "hub.verify_token": "",
                                            "hub.challenge": "1"})  # fmt: skip
    assert r.status_code == 403


# ------------------------------------------------------------------ signature
def test_bad_signature(meta_client: TestClient, setup: Any, db_session: Session) -> None:
    r = post(meta_client, envelope([text_msg("wamid.1", "ey")]), secret="nope")
    assert r.status_code == 401
    assert (
        db_session.scalars(select(MessageLog).where(MessageLog.direction == Direction.IN)).all()
        == []
    )


def test_missing_signature(meta_client: TestClient) -> None:
    assert meta_client.post("/webhook/meta", json=envelope()).status_code == 401


# ------------------------------------------------------------------- messages
def test_incoming_text_stored(meta_client: TestClient, setup: Any, db_session: Session) -> None:
    _shop, customer, order = setup
    r = post(meta_client, envelope([text_msg("wamid.IN1", "ey nconfirmi")]))
    assert r.status_code == 200 and r.json() == {"status": "ok"}

    db_session.expire_all()
    msg = db_session.scalars(
        select(MessageLog).where(MessageLog.wa_message_id == "wamid.IN1")
    ).one()
    assert msg.direction is Direction.IN
    assert msg.content == "ey nconfirmi"
    assert msg.order_id == order.id
    assert msg.customer_id == customer.id
    assert customer.last_inbound_at is not None


def test_button_reply_links_payload_order(
    meta_client: TestClient, setup: Any, db_session: Session
) -> None:
    shop, customer, order = setup
    older = make_order(db_session, shop, customer, status=OrderStatus.AWAITING_CONFIRMATION)
    db_session.commit()
    button = {"from": "21698123456", "id": "wamid.BTN", "timestamp": "1", "type": "button",
              "button": {"payload": f"CONFIRM:{older.id}", "text": "✅ Nconfirmi"}}  # fmt: skip
    post(meta_client, envelope([button]))
    db_session.expire_all()
    msg = db_session.scalars(
        select(MessageLog).where(MessageLog.wa_message_id == "wamid.BTN")
    ).one()
    assert msg.type is MessageType.BUTTON
    assert msg.payload == f"CONFIRM:{older.id}"
    assert msg.order_id == older.id


def test_payload_of_other_customer_is_not_trusted(meta_client: TestClient, setup: Any,
                                                  db_session: Session) -> None:  # fmt: skip
    shop, _customer, order = setup
    other_order = make_order(db_session, shop, status=OrderStatus.AWAITING_CONFIRMATION)
    db_session.commit()
    button = {"from": "21698123456", "id": "wamid.EVIL", "timestamp": "1", "type": "interactive",
              "interactive": {"type": "button_reply",
                              "button_reply": {"id": f"CANCEL:{other_order.id}", "title": "x"}}}  # fmt: skip
    post(meta_client, envelope([button]))
    msg = db_session.scalars(
        select(MessageLog).where(MessageLog.wa_message_id == "wamid.EVIL")
    ).one()
    assert msg.order_id == order.id  # falls back to the sender's own open order


def test_duplicate_delivery_stored_once(
    meta_client: TestClient, setup: Any, db_session: Session
) -> None:
    payload = envelope([text_msg("wamid.DUP", "ey")])
    post(meta_client, payload)
    post(meta_client, payload)
    assert (
        len(
            db_session.scalars(
                select(MessageLog).where(MessageLog.wa_message_id == "wamid.DUP")
            ).all()
        )
        == 1
    )


def test_unknown_sender_and_unknown_number_ignored(meta_client: TestClient, setup: Any,
                                                   db_session: Session) -> None:  # fmt: skip
    assert (
        post(
            meta_client, envelope([text_msg("wamid.U1", "salam", sender="21655555555")])
        ).status_code
        == 200
    )
    assert (
        post(meta_client, envelope([text_msg("wamid.U2", "salam")], pnid="000")).status_code == 200
    )
    assert (
        db_session.scalars(select(MessageLog).where(MessageLog.direction == Direction.IN)).all()
        == []
    )


@pytest.mark.parametrize("payload", [{"object": "page", "entry": []}, {"foo": 1}, {"object": "whatsapp_business_account",
                                     "entry": [{"changes": [{"value": {"metadata": {}}}]}]}])  # fmt: skip
def test_invalid_payloads_ignored(meta_client: TestClient, payload: dict[str, Any]) -> None:
    r = post(meta_client, payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


# ------------------------------------------------------------------- statuses
def _out_msg(
    db: Session, order: Any, wamid: str, status: MessageStatus = MessageStatus.SENT
) -> MessageLog:
    msg = MessageLog(order=order, direction=Direction.OUT, type=MessageType.TEMPLATE, content="x",
                     wa_message_id=wamid, status=status)  # fmt: skip
    db.add(msg)
    db.commit()
    return msg


def test_statuses_move_forward_only(
    meta_client: TestClient, setup: Any, db_session: Session
) -> None:
    msg = _out_msg(db_session, setup[2], "wamid.OUT")
    post(
        meta_client, envelope(statuses=[{"id": "wamid.OUT", "status": "read", "recipient_id": "x"}])
    )
    post(
        meta_client,
        envelope(statuses=[{"id": "wamid.OUT", "status": "delivered", "recipient_id": "x"}]),
    )
    db_session.expire_all()
    assert msg.status is MessageStatus.READ


def test_failed_status_records_error(
    meta_client: TestClient, setup: Any, db_session: Session
) -> None:
    msg = _out_msg(db_session, setup[2], "wamid.F")
    post(meta_client, envelope(statuses=[{"id": "wamid.F", "status": "failed", "recipient_id": "x",
                                          "errors": [{"code": 131026, "title": "Message undeliverable"}]}]))  # fmt: skip
    db_session.expire_all()
    assert msg.status is MessageStatus.FAILED
    assert msg.error == "131026: Message undeliverable"


def test_end_to_end_reply_confirms_order(
    meta_client: TestClient, setup: Any, db_session: Session
) -> None:
    """Webhook -> stored -> intent -> order CONFIRMED -> thank-you reply logged (DRY_RUN)."""
    _shop, _customer, order = setup
    post(meta_client, envelope([text_msg("wamid.E2E", "Eyyy nconfirmi 👍")]))
    db_session.expire_all()
    assert order.status is OrderStatus.CONFIRMED
    out = [mm for mm in order.messages if mm.direction is Direction.OUT]
    assert out and out[-1].content.startswith("Ya3tik essa7a")
    assert out[-1].wa_message_id.startswith("dryrun.")  # type: ignore[union-attr]
