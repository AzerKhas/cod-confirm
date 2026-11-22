from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import OrderStatus
from app.models import Order, Shop
from app.services.voice import apply_call_result, send_voice_calls, verify_signature
from tests.factories import make_customer, make_order, make_shop

CALL_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
SECRET = "wsec_test"
NOW = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"ELEVENLABS_API_KEY": "xi-key", "ELEVENLABS_AGENT_ID": "agent_1",
                 "ELEVENLABS_PHONE_NUMBER_ID": "phnum_1",
                 "ELEVENLABS_WEBHOOK_SECRET": SECRET}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def unanswered(db: Session, shop: Shop, phone: str = "+21655444333", **kw: object) -> Order:
    return make_order(db, shop, make_customer(db, shop, phone_e164=phone), OrderStatus.NO_ANSWER,
                      **{"created_at": NOW - timedelta(hours=30), **kw})  # fmt: skip


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> str:
    ts = ts or int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def result(conversation_id: str, decision: str) -> dict:
    return {"type": "post_call_transcription", "event_timestamp": 1,
            "data": {"conversation_id": conversation_id, "status": "done",
                     "analysis": {"call_successful": "success", "transcript_summary": "",
                                  "data_collection_results": {"decision": {
                                      "data_collection_id": "decision", "value": decision,
                                      "rationale": "r"}}}}}  # fmt: skip


# ---------------------------------------------------------------- calls
@respx.mock
def test_calls_unanswered_orders_once(db_session: Session) -> None:
    route = respx.post(CALL_URL).mock(return_value=httpx.Response(
        200, json={"success": True, "message": "ok", "conversation_id": "conv_1", "callSid": "CA1"}))  # fmt: skip
    shop = make_shop(db_session, name="Nour Mode", voice_calls_on=True)
    order = unanswered(db_session, shop, city="Sousse")
    make_order(db_session, shop, make_customer(db_session, shop, phone_e164="+21655444334"),
               OrderStatus.AWAITING_CONFIRMATION, created_at=NOW)  # still on WhatsApp  # fmt: skip
    db_session.commit()

    assert send_voice_calls(db_session, shop, NOW) == 1
    req = route.calls.last.request
    assert req.headers["xi-api-key"] == "xi-key"
    body = json.loads(req.content)
    assert body["agent_id"] == "agent_1" and body["agent_phone_number_id"] == "phnum_1"
    assert body["to_number"] == "+21655444333"
    variables = body["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["shop_name"] == "Nour Mode" and variables["city"] == "Sousse"
    assert variables["products"] == "1 x Skmei 1251" and variables["total"] == "59"
    assert order.call_state == "calling" and order.call_conversation_id == "conv_1"
    assert send_voice_calls(db_session, shop, NOW) == 0 and route.call_count == 1


@respx.mock
def test_no_call_when_off_opted_out_or_old(db_session: Session) -> None:
    route = respx.post(CALL_URL).mock(return_value=httpx.Response(200, json={}))
    off = make_shop(db_session, slug="off")
    unanswered(db_session, off)
    shop = make_shop(db_session, slug="on", voice_calls_on=True)
    stop = unanswered(db_session, shop, phone="+21655444001")
    stop.customer.opted_out = True
    unanswered(db_session, shop, phone="+21655444002", created_at=NOW - timedelta(days=5))
    db_session.commit()
    assert send_voice_calls(db_session, off, NOW) == 0
    assert send_voice_calls(db_session, shop, NOW) == 0
    assert stop.call_state == "failed" and route.call_count == 0


@respx.mock
def test_provider_errors(db_session: Session) -> None:
    route = respx.post(CALL_URL).mock(return_value=httpx.Response(503))
    shop = make_shop(db_session, voice_calls_on=True)
    order = unanswered(db_session, shop)
    db_session.commit()
    send_voice_calls(db_session, shop, NOW)
    assert order.call_state is None  # retried next tick
    route.mock(return_value=httpx.Response(422, json={"detail": []}))
    send_voice_calls(db_session, shop, NOW)
    assert order.call_state == "failed"  # never loops on a bad request


def test_not_configured_means_no_call(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    get_settings.cache_clear()
    shop = make_shop(db_session, voice_calls_on=True)
    unanswered(db_session, shop)
    assert send_voice_calls(db_session, shop, NOW) == 0


# -------------------------------------------------------------- results
@pytest.mark.parametrize(
    ("decision", "status"),
    [("confirmed", OrderStatus.CONFIRMED), ("cancelled", OrderStatus.CANCELLED),
     ("unclear", OrderStatus.NO_ANSWER), ("", OrderStatus.NO_ANSWER)],
)  # fmt: skip
def test_call_result_updates_order(db_session: Session, decision: str, status: OrderStatus) -> None:
    shop = make_shop(db_session)
    order = unanswered(db_session, shop, call_state="calling", call_conversation_id="conv_9")
    db_session.commit()
    apply_call_result(db_session, result("conv_9", decision))
    assert order.status is status and order.call_state == "done"
    if status is OrderStatus.CONFIRMED:
        assert order.events[-1].note == "appel vocal"
    assert apply_call_result(db_session, result("conv_9", "cancelled")) == "ignored"  # once


def test_whatsapp_answer_wins_over_late_call_result(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = unanswered(db_session, shop, call_state="calling", call_conversation_id="conv_9")
    order.status = OrderStatus.CONFIRMED  # customer answered on WhatsApp meanwhile
    db_session.commit()
    apply_call_result(db_session, result("conv_9", "cancelled"))
    assert order.status is OrderStatus.CONFIRMED


def test_initiation_failure_and_unknown_call(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = unanswered(db_session, shop, call_state="calling", call_conversation_id="conv_9")
    db_session.commit()
    assert apply_call_result(db_session, result("conv_other", "confirmed")) == "ignored"
    fail = {"type": "call_initiation_failure",
            "data": {"conversation_id": "conv_9", "failure_reason": "no-answer"}}  # fmt: skip
    assert apply_call_result(db_session, fail) == "failed"
    assert order.call_state == "failed" and order.status is OrderStatus.NO_ANSWER


# ------------------------------------------------------------ signature
def test_signature() -> None:
    body = b'{"type":"x"}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body + b" ", sign(body))
    assert not verify_signature(SECRET, body, sign(body, secret="other"))
    assert not verify_signature(SECRET, body, sign(body, ts=int(time.time()) - 3600))  # replay
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature("", body, sign(body, secret=""))
    assert not verify_signature(SECRET, body, "garbage")


def test_webhook_endpoint(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    order = unanswered(db_session, shop, call_state="calling", call_conversation_id="conv_9")
    db_session.commit()
    body = json.dumps(result("conv_9", "confirmed")).encode()
    bad = client.post("/webhooks/voice", content=body, headers={"ElevenLabs-Signature": "t=1,v0=x"})
    assert bad.status_code == 401
    r = client.post("/webhooks/voice", content=body, headers={"ElevenLabs-Signature": sign(body)})
    assert r.status_code == 200 and r.json() == {"result": "confirmed"}
    db_session.refresh(order)
    assert order.status is OrderStatus.CONFIRMED
