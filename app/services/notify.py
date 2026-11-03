"""Business-initiated WhatsApp messages about an order (templates).

Every send is logged in MessageLog. Opted-out customers are never contacted.
Failures never raise to the caller: the MessageLog is marked FAILED and the order keeps
its status, so the scheduler can retry later.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.enums import Actor, Direction, Language, MessageStatus, MessageType, OrderStatus
from app.models import Customer, MessageLog, Order
from app.services import messages as m
from app.services.orders import transition
from app.services.whatsapp import WhatsAppClient, WhatsAppError, client_for_shop

log = logging.getLogger(__name__)


def customer_language(customer: Customer) -> Language:
    return customer.language or customer.shop.default_language


def template_language_code(language: Language, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return {
        Language.DARIJA: settings.whatsapp_darija_template_language,
        Language.ARABIC: "ar",
        Language.FRENCH: "fr",
    }[language]


def button_payloads(order: Order) -> list[str]:
    return [f"{action}:{order.id}" for action in m.BUTTON_ACTIONS]


def _params(tpl: m.WaTemplate, order: Order) -> list[str]:
    customer, shop = order.customer, order.shop
    common = {
        "customer first name": m.first_name(customer.name),
        "shop name": m.clean_param(shop.name, 60),
        "items summary": m.items_summary(order.items),
        "total": m.format_amount(order.total),
        "city": m.clean_param(order.city, 60),
    }
    return [common[v] for v in tpl.variables]


def send_order_template(
    db: Session,
    order: Order,
    tpl: m.WaTemplate,
    client: WhatsAppClient | None = None,
) -> MessageLog | None:
    """Send `tpl` for `order`. Returns the MessageLog, or None if the customer opted out."""
    customer = order.customer
    if customer.opted_out:
        log.info("notify: customer opted out, not sent", extra={"order_id": order.id})
        return None

    language = customer_language(customer)
    params = _params(tpl, order)
    payloads: Sequence[str] = button_payloads(order) if tpl.buttons else ()
    msg = MessageLog(
        order=order,
        customer_id=customer.id,
        direction=Direction.OUT,
        type=MessageType.TEMPLATE,
        content=tpl.render(language, params),
        status=MessageStatus.QUEUED,
    )
    db.add(msg)

    try:
        client = client or client_for_shop(order.shop)
        result = client.send_template(
            customer.phone_e164,
            tpl.name(language),
            template_language_code(language),
            body_params=params,
            button_payloads=payloads,
        )
    except WhatsAppError as exc:
        msg.status = MessageStatus.FAILED
        msg.error = str(exc)[:500]
        log.warning("notify: send failed", extra={"order_id": order.id, "error": msg.error})
        db.flush()
        return msg

    msg.wa_message_id = result.message_id
    msg.status = MessageStatus.SENT
    order.last_contacted_at = datetime.now(UTC)
    db.flush()
    return msg


def send_order_confirmation(
    db: Session, order: Order, client: WhatsAppClient | None = None
) -> MessageLog | None:
    """NEW -> AWAITING_CONFIRMATION once the confirmation template is sent. Commits."""
    if order.status is not OrderStatus.NEW:
        return None
    msg = send_order_template(db, order, m.ORDER_CONFIRMATION, client)
    if msg is not None and msg.status is MessageStatus.SENT:
        transition(db, order, OrderStatus.AWAITING_CONFIRMATION, Actor.BOT)
    db.commit()
    return msg


def send_order_confirmation_job(order_id: int) -> None:
    """Background task entry point: own DB session (the request one is closed)."""
    from app import db as db_module
    from app.services.scheduler import is_quiet_hours

    with db_module.SessionLocal() as db:
        order = db.get(Order, order_id)
        if order is None:
            return
        if is_quiet_hours(order.shop, datetime.now(UTC)):
            log.info("notify: quiet hours, confirmation postponed", extra={"order_id": order_id})
            return  # the scheduler sends it at the end of the quiet hours
        try:
            send_order_confirmation(db, order)
        except Exception:  # never crash the worker; the scheduler retries NEW orders
            log.exception("notify: confirmation job failed", extra={"order_id": order_id})
            db.rollback()
