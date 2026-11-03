"""Meta webhook payloads: parsing (validated) and storage of messages / delivery statuses."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Direction, MessageStatus, MessageType, OrderStatus
from app.models import Customer, MessageLog, Order, Shop
from app.security import mask_phone

log = logging.getLogger(__name__)


# ------------------------------------------------------------ payload models
class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WaText(_M):
    body: str = Field(default="", max_length=4096)


class WaButton(_M):  # reply to a template quick-reply button
    payload: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=256)


class WaButtonReply(_M):
    id: str = Field(default="", max_length=256)
    title: str = Field(default="", max_length=256)


class WaInteractive(_M):
    type: str = ""
    button_reply: WaButtonReply | None = None


class WaMessage(_M):
    id: str = Field(max_length=128)
    from_: str = Field(alias="from", max_length=20, pattern=r"^\d{6,20}$")
    timestamp: str = ""
    type: str = Field(max_length=32)
    text: WaText | None = None
    button: WaButton | None = None
    interactive: WaInteractive | None = None

    def content_and_payload(self) -> tuple[str, str | None, MessageType]:
        if self.type == "text" and self.text:
            return self.text.body, None, MessageType.TEXT
        if self.type == "button" and self.button:
            return self.button.text, self.button.payload or None, MessageType.BUTTON
        if self.type == "interactive" and self.interactive and self.interactive.button_reply:
            br = self.interactive.button_reply
            return br.title, br.id or None, MessageType.BUTTON
        return f"[{self.type}]", None, MessageType.TEXT  # image, audio, sticker...


class WaStatusError(_M):
    code: int | None = None
    title: str = ""


class WaStatus(_M):
    id: str = Field(max_length=128)
    status: Literal["sent", "delivered", "read", "failed", "deleted", "warning"] | str
    recipient_id: str = ""
    errors: list[WaStatusError] = Field(default_factory=list)


class WaMetadata(_M):
    phone_number_id: str = Field(max_length=40)
    display_phone_number: str = ""


class WaValue(_M):
    messaging_product: str = "whatsapp"
    metadata: WaMetadata
    messages: list[WaMessage] = Field(default_factory=list, max_length=100)
    statuses: list[WaStatus] = Field(default_factory=list, max_length=100)


class WaChange(_M):
    field: str = ""
    value: WaValue


class WaEntry(_M):
    id: str = ""
    changes: list[WaChange] = Field(default_factory=list, max_length=100)


class WaWebhook(_M):
    object: str
    entry: list[WaEntry] = Field(default_factory=list, max_length=100)


# ------------------------------------------------------------------ storage
_STATUS_RANK = {
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
}

# Orders a customer reply can refer to (most recent first).
OPEN_STATUSES = (
    OrderStatus.AWAITING_CONFIRMATION,
    OrderStatus.NO_ANSWER,
    OrderStatus.MODIFY_REQUESTED,
    OrderStatus.NEW,
    OrderStatus.CONFIRMED,
    OrderStatus.SHIPPED,
)


def apply_status(db: Session, st: WaStatus) -> MessageLog | None:
    """Update an outgoing message status. Statuses only move forward (sent<delivered<read)."""
    msg = db.scalar(select(MessageLog).where(MessageLog.wa_message_id == st.id))
    if msg is None:
        return None
    try:
        new = MessageStatus(st.status)
    except ValueError:
        return msg
    if new is MessageStatus.FAILED:
        msg.status = new
        if st.errors:
            err = st.errors[0]
            msg.error = f"{err.code}: {err.title}"[:500]
    elif msg.status is not MessageStatus.FAILED and _STATUS_RANK.get(new, -1) > _STATUS_RANK.get(
        msg.status, -1
    ):
        msg.status = new
    return msg


def latest_open_order(db: Session, customer: Customer) -> Order | None:
    return db.scalar(
        select(Order)
        .where(Order.customer_id == customer.id, Order.status.in_(OPEN_STATUSES))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )


def order_from_payload(db: Session, customer: Customer, payload: str | None) -> Order | None:
    """Button payload 'CONFIRM:42' -> order 42, only if it belongs to this customer."""
    if not payload or ":" not in payload:
        return None
    _action, _, raw_id = payload.partition(":")
    if not raw_id.isdigit():
        return None
    order = db.get(Order, int(raw_id))
    return order if order is not None and order.customer_id == customer.id else None


def store_message(db: Session, shop: Shop, wa: WaMessage) -> MessageLog | None:
    """Store an incoming message. Returns None for unknown senders or duplicates."""
    if db.scalar(select(MessageLog.id).where(MessageLog.wa_message_id == wa.id)):
        return None  # Meta re-delivered the same webhook

    phone = "+" + wa.from_
    customer = db.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.phone_e164 == phone)
    )
    if customer is None:
        log.info(
            "inbound: message from unknown number",
            extra={"shop_id": shop.id, "phone": mask_phone(phone)},
        )
        return None

    content, payload, mtype = wa.content_and_payload()
    order = order_from_payload(db, customer, payload) or latest_open_order(db, customer)
    customer.last_inbound_at = datetime.now(UTC)
    msg = MessageLog(
        order=order,
        customer_id=customer.id,
        direction=Direction.IN,
        type=mtype,
        content=content,
        payload=payload,
        wa_message_id=wa.id,
        status=MessageStatus.RECEIVED,
    )
    db.add(msg)
    db.flush()
    return msg


def shop_for_phone_number_id(db: Session, phone_number_id: str) -> Shop | None:
    return db.scalar(select(Shop).where(Shop.whatsapp_phone_number_id == phone_number_id))


def iter_values(hook: WaWebhook) -> list[WaValue]:
    return [c.value for e in hook.entry for c in e.changes if c.field in ("messages", "")]


def summarize(values: list[WaValue]) -> dict[str, Any]:
    return {
        "messages": sum(len(v.messages) for v in values),
        "statuses": sum(len(v.statuses) for v in values),
    }
