"""AI phone call (Tunisian Darija) for orders still unanswered on WhatsApp.

The voice agent lives in ElevenLabs (prompt, voice, data collection: see docs/voice-agent.md);
this module only starts the call and applies its result:

    POST https://api.elevenlabs.io/v1/convai/{twilio|sip-trunk}/outbound-call   (xi-api-key)
    {"agent_id", "agent_phone_number_id", "to_number",
     "conversation_initiation_client_data": {"dynamic_variables": {...}}}
    -> {"success", "message", "conversation_id", ...}

After the call ElevenLabs POSTs a `post_call_transcription` (or `call_initiation_failure`)
webhook signed with `ElevenLabs-Signature: t=<unix>,v0=<hex HMAC-SHA256(secret, "t.body")>`.
The agent's verdict is the data-collection item `decision`: confirmed / cancelled / unclear.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import Actor, OrderStatus
from app.models import Order, Shop
from app.services.orders import transition

log = logging.getLogger(__name__)

API_URL = "https://api.elevenlabs.io/v1/convai"
SIGNATURE_TOLERANCE_S = 30 * 60  # same window as the official SDK
MAX_ORDER_AGE = timedelta(days=3)  # a call about a week-old order makes no sense
CALLS_PER_TICK = 5
DECISIONS = {"confirmed": OrderStatus.CONFIRMED, "cancelled": OrderStatus.CANCELLED}


class VoiceError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


def voice_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(
        s.elevenlabs_api_key.get_secret_value()
        and s.elevenlabs_agent_id
        and s.elevenlabs_phone_number_id
    )


def call_variables(order: Order) -> dict[str, Any]:
    """Dynamic variables used by the agent's prompt ({{customer_name}}...)."""
    products = ", ".join(
        f"{item.get('qty', 1)} x {item.get('name', '')}" for item in order.items or []
    )
    return {
        "customer_name": order.customer.name,
        "shop_name": order.shop.name,
        "products": products,
        "total": f"{order.total:.3f}".rstrip("0").rstrip("."),
        "city": order.city,
        "address": order.address,
        "order_id": str(order.id),
    }


def place_call(order: Order, settings: Settings | None = None) -> str:
    """Start the call; returns the ElevenLabs conversation id. Raises VoiceError."""
    s = settings or get_settings()
    body = {
        "agent_id": s.elevenlabs_agent_id,
        "agent_phone_number_id": s.elevenlabs_phone_number_id,
        "to_number": order.customer.phone_e164,
        "conversation_initiation_client_data": {"dynamic_variables": call_variables(order)},
    }
    try:
        r = httpx.post(
            f"{API_URL}/{s.elevenlabs_telephony}/outbound-call",
            headers={"xi-api-key": s.elevenlabs_api_key.get_secret_value()},
            json=body,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise VoiceError(f"network: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise VoiceError(f"HTTP {r.status_code}: {r.text[:200]}", r.status_code)
    data = r.json()
    if not data.get("success") or not data.get("conversation_id"):
        raise VoiceError(f"refused: {str(data.get('message', ''))[:200]}", 400)
    return str(data["conversation_id"])


def pending_calls(db: Session, shop: Shop, now: datetime) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(
                Order.shop_id == shop.id,
                Order.status == OrderStatus.NO_ANSWER,
                Order.call_state.is_(None),
                Order.created_at >= now - MAX_ORDER_AGE,
            )
            .order_by(Order.id)
            .limit(CALLS_PER_TICK)
        )
    )


def send_voice_calls(db: Session, shop: Shop, now: datetime) -> int:
    """Scheduler job (outside quiet hours): one call per order unanswered on WhatsApp."""
    if not shop.voice_calls_on or not voice_configured():
        return 0
    placed = 0
    for order in pending_calls(db, shop, now):
        if order.customer.opted_out:  # STOP on WhatsApp = never contact again
            order.call_state = "failed"
            continue
        try:
            order.call_conversation_id = place_call(order)
            order.call_state = "calling"
            placed += 1
        except VoiceError as exc:
            log.warning("voice: call failed", extra={"order_id": order.id, "error": str(exc)})
            if not exc.permanent:
                break  # provider down: retry next tick
            order.call_state = "failed"
        finally:
            db.commit()
    return placed


def verify_signature(
    secret: str, body: bytes, header: str | None, now: float | None = None
) -> bool:
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t", ""), parts.get("v0", "")
    if not ts.isdigit() or not sig:
        return False
    if (now or time.time()) - int(ts) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def apply_call_result(db: Session, payload: dict[str, Any]) -> str:
    """Apply one webhook event; idempotent (a call is applied once). Returns what happened."""
    data = payload.get("data") or {}
    conversation_id = str(data.get("conversation_id") or "")
    order = (
        db.scalar(select(Order).where(Order.call_conversation_id == conversation_id))
        if conversation_id
        else None
    )
    if order is None or order.call_state != "calling":
        return "ignored"
    if payload.get("type") == "call_initiation_failure":
        order.call_state = "failed"
        db.commit()
        return "failed"
    if payload.get("type") != "post_call_transcription":
        return "ignored"

    results = (data.get("analysis") or {}).get("data_collection_results") or {}
    decision = str((results.get("decision") or {}).get("value") or "").strip().lower()
    order.call_state = "done"
    target = DECISIONS.get(decision)
    outcome = "unclear"
    # The customer may have answered on WhatsApp meanwhile: never overwrite that.
    if target and order.status is OrderStatus.NO_ANSWER:
        transition(db, order, target, Actor.CUSTOMER, note="appel vocal")
        outcome = decision
    db.commit()
    log.info("voice: call result", extra={"order_id": order.id, "outcome": outcome})
    return outcome
