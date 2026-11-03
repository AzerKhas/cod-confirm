"""Conversation logic: what the bot does with each incoming customer message.

    STOP      -> opt-out (never contacted again), one last acknowledgment
    START     -> opt back in
    CONFIRM   -> order CONFIRMED + thank-you
    CANCEL    -> order CANCELLED + acknowledgment
    MODIFY    -> order MODIFY_REQUESTED + owner alerted + "tell us what to change"
    QUESTION  -> owner alerted + "the shop will answer"
    UNKNOWN   -> polite clarification with buttons (at most `max_clarifications` per order)

Free-form replies are only sent inside the 24h customer-service window (the customer just
wrote to us, so it is open; it is still checked). Sending failures are logged, never raised.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import Actor, Direction, Language, MessageStatus, MessageType, OrderStatus
from app.models import Customer, MessageLog, Order, Shop
from app.security import mask_phone
from app.services import messages as m
from app.services import usage
from app.services.alerts import notify_owner
from app.services.intent import Intent, IntentClassifier, build_classifier, detect
from app.services.notify import customer_language
from app.services.orders import can_transition, transition
from app.services.whatsapp import ReplyButton, WhatsAppClient, WhatsAppError, client_for_shop

log = logging.getLogger(__name__)

SERVICE_WINDOW = timedelta(hours=24)
S = OrderStatus

_ARABIC_CHARS = range(0x0600, 0x0700)


def in_service_window(customer: Customer, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return customer.last_inbound_at is not None and now - customer.last_inbound_at < SERVICE_WINDOW


def _looks_arabic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(ord(c) in _ARABIC_CHARS for c in letters) / len(letters) > 0.5


class Conversation:
    """Handles one incoming message. `client` / `classifier` are injectable for tests."""

    def __init__(
        self,
        db: Session,
        shop: Shop,
        msg: MessageLog,
        client: WhatsAppClient | None = None,
        classifier: IntentClassifier | None = None,
    ) -> None:
        self.db = db
        self.shop = shop
        self.msg = msg
        self.customer: Customer = db.get(Customer, msg.customer_id)  # type: ignore[assignment]
        self.order: Order | None = msg.order
        self._client = client
        self.classifier = classifier

    # ------------------------------------------------------------- helpers
    @property
    def client(self) -> WhatsAppClient:
        if self._client is None:
            self._client = client_for_shop(self.shop)
        return self._client

    @property
    def language(self) -> Language:
        return customer_language(self.customer)

    def _text(self, key: str) -> str:
        return m.reply_text(
            self.language, key, name=m.first_name(self.customer.name), shop=self.shop.name
        )

    def _log_out(self, content: str, mtype: MessageType) -> MessageLog:
        out = MessageLog(
            order=self.order,
            customer_id=self.customer.id,
            direction=Direction.OUT,
            type=mtype,
            content=content,
            status=MessageStatus.QUEUED,
        )
        self.db.add(out)
        return out

    def reply(self, key: str, *, with_buttons: bool = False, force: bool = False) -> None:
        """Send a free-form reply. `force` is only used for the final STOP acknowledgment."""
        if self.customer.opted_out and not force:
            return
        if not self.shop.is_active:
            return
        if not in_service_window(self.customer):
            log.info("conversation: 24h window closed, reply skipped", extra={"key": key})
            return
        text = self._text(key)
        out = self._log_out(text, MessageType.BUTTON if with_buttons else MessageType.TEXT)
        try:
            if with_buttons and self.order is not None:
                titles = m.REPLY_BUTTONS[self.language]
                actions = m.BUTTON_ACTIONS
                buttons = [
                    ReplyButton(f"{a}:{self.order.id}", t)
                    for a, t in zip(actions, titles, strict=True)
                ]
                result = self.client.send_buttons(self.customer.phone_e164, text, buttons)
            else:
                result = self.client.send_text(self.customer.phone_e164, text)
        except WhatsAppError as exc:
            out.status = MessageStatus.FAILED
            out.error = str(exc)[:500]
            log.warning("conversation: reply failed", extra={"key": key, "error": out.error})
            return
        out.wa_message_id = result.message_id
        out.status = MessageStatus.SENT
        usage.bump(self.db, self.shop.id, messages=1)

    def alert_owner(self, title: str) -> None:
        order = self.order
        lines = [
            f"🔔 {self.shop.name} – {title}",
            f"Client : {self.customer.name} ({self.customer.phone_e164})",
        ]
        if order is not None:
            total = f"{m.format_amount(order.total)} {order.currency}"
            lines.append(
                f"Commande #{order.id} – {m.items_summary(order.items)} – {total} – {order.city}"
            )
        lines.append(f"Message : {self.msg.content[:500]}")
        notify_owner(self.shop, "\n".join(lines))

    def _move(self, to: OrderStatus) -> bool:
        if self.order is None or not can_transition(self.order.status, to, Actor.CUSTOMER):
            return False
        transition(self.db, self.order, to, Actor.CUSTOMER, note=f"reply: {self.msg.content[:200]}")
        return True

    def _clarifications_sent(self) -> int:
        if self.order is None:
            return 0
        clarify_text = self._text("clarify")
        return (
            self.db.scalar(
                select(func.count())
                .select_from(MessageLog)
                .where(MessageLog.order_id == self.order.id, MessageLog.content == clarify_text)
            )
            or 0
        )

    def _last_out_content(self) -> str | None:
        if self.order is None:
            return None
        return self.db.scalar(
            select(MessageLog.content)
            .where(MessageLog.order_id == self.order.id, MessageLog.direction == Direction.OUT)
            .order_by(MessageLog.id.desc())
            .limit(1)
        )

    # ---------------------------------------------------------------- main
    def handle(self) -> Intent:
        msg, customer = self.msg, self.customer

        if customer.language is None and _looks_arabic(msg.content):
            customer.language = Language.ARABIC

        try:
            if msg.wa_message_id:
                self.client.mark_as_read(msg.wa_message_id)
        except WhatsAppError:
            pass  # blue ticks are nice to have, never critical

        intent = detect(msg.content, msg.payload, self.classifier)
        log.info(
            "conversation: intent",
            extra={"intent": intent.value, "order_id": self.order.id if self.order else None,
                   "phone": mask_phone(customer.phone_e164)},
        )  # fmt: skip

        if intent is Intent.STOP:
            if not customer.opted_out:
                customer.opted_out = True
                customer.opted_out_at = datetime.now(UTC)
                self.reply("stopped", force=True)
            return intent
        if intent is Intent.START:
            if customer.opted_out:
                customer.opted_out = False
                customer.opted_out_at = None
                self.reply("resubscribed")
            return intent
        if customer.opted_out:
            return intent  # stored for the owner, no automatic answer

        if self.order is None:
            if intent is not Intent.UNKNOWN or self._last_out_content() is None:
                self.reply("no_order")
            return intent

        status = self.order.status
        if intent is Intent.CONFIRM:
            if self._move(S.CONFIRMED):
                self.reply("confirmed")
            elif status in (S.CONFIRMED, S.SHIPPED, S.DELIVERED):
                self.reply("already_confirmed")
            elif status is S.CANCELLED:
                self.reply("already_cancelled")
            elif status is S.MODIFY_REQUESTED:
                self.reply("modify_ack")
        elif intent is Intent.CANCEL:
            if status is S.SHIPPED:
                self.reply("shipped_locked")
                self.alert_owner("veut annuler une commande déjà expédiée")
            elif self._move(S.CANCELLED):
                self.reply("cancelled")
                self.alert_owner("commande annulée par le client")
            elif status is S.CANCELLED:
                self.reply("already_cancelled")
        elif intent is Intent.MODIFY:
            if self._move(S.MODIFY_REQUESTED):
                self.reply("modify_ask")
                self.alert_owner("demande de modification")
            elif status in (S.CONFIRMED, S.SHIPPED):
                self.reply("shipped_locked" if status is S.SHIPPED else "modify_ack")
                self.alert_owner("demande de modification")
            elif status is S.MODIFY_REQUESTED:
                self.reply("modify_ack")
                self.alert_owner("précision sur la modification")
        elif intent is Intent.QUESTION:
            self.reply("question_ack")
            self.alert_owner("question du client")
        elif status is S.MODIFY_REQUESTED:
            # Free text after "what do you want to change?" = the change itself.
            if self._last_out_content() != self._text("modify_ack"):
                self.reply("modify_ack")
            self.alert_owner("précision sur la modification")
        elif status in (S.AWAITING_CONFIRMATION, S.NO_ANSWER):
            if self._clarifications_sent() < get_settings().max_clarifications:
                self.reply("clarify", with_buttons=True)
            else:
                self.alert_owner("réponse non comprise")
        return intent


def handle_incoming(
    db: Session,
    shop: Shop,
    msg: MessageLog,
    client: WhatsAppClient | None = None,
    classifier: IntentClassifier | None = None,
) -> Intent:
    """Entry point called by the Meta webhook for each new incoming message."""
    return Conversation(db, shop, msg, client, classifier or build_classifier()).handle()
