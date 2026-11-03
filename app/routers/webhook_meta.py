"""Meta (WhatsApp Cloud API) webhook.

GET  /webhook/meta  verification handshake (hub.verify_token)
POST /webhook/meta  incoming messages + delivery statuses, signed with X-Hub-Signature-256

Always answers 200 quickly once the signature is valid (Meta retries otherwise);
processing errors are logged, never returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.routers.intake import read_limited_body
from app.services import inbound

router = APIRouter(tags=["whatsapp"])
log = logging.getLogger(__name__)


def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex HMAC-SHA256(app_secret, raw body)>'."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip())


@router.get("/webhook/meta", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge", max_length=200),
    settings: Settings = Depends(get_settings),
) -> str:
    expected = settings.meta_verify_token.get_secret_value()
    if (
        hub_mode == "subscribe"
        and expected
        and hmac.compare_digest(hub_verify_token.encode(), expected.encode())
    ):
        return hub_challenge
    raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")


def process_webhook(db: Session, hook: inbound.WaWebhook) -> list[int]:
    """Store statuses + messages; returns ids of newly stored incoming MessageLogs."""
    from app.services.conversation import handle_incoming  # Phase 4

    new_ids: list[int] = []
    for value in inbound.iter_values(hook):
        for st in value.statuses:
            inbound.apply_status(db, st)
        if not value.messages:
            continue
        shop = inbound.shop_for_phone_number_id(db, value.metadata.phone_number_id)
        if shop is None:
            log.warning(
                "meta: unknown phone_number_id", extra={"pnid": value.metadata.phone_number_id}
            )
            continue
        for wa in value.messages:
            msg = inbound.store_message(db, shop, wa)
            if msg is not None:
                new_ids.append(msg.id)
                handle_incoming(db, shop, msg)
    db.commit()
    return new_ids


@router.post("/webhook/meta")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    body = await read_limited_body(request)
    if not verify_meta_signature(
        settings.meta_app_secret.get_secret_value(), body, x_hub_signature_256
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        hook = inbound.WaWebhook.model_validate(json.loads(body))
    except (ValueError, ValidationError):
        log.warning("meta: invalid webhook payload")
        return {"status": "ignored"}

    if hook.object != "whatsapp_business_account":
        return {"status": "ignored"}

    try:
        await run_in_threadpool(process_webhook, db, hook)
    except Exception:  # never make Meta retry because of our own bug
        log.exception("meta: webhook processing failed")
        db.rollback()
    return {"status": "ok"}
