"""AI voice calls: ElevenLabs post-call webhook and the shop owner's on/off switch."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import csrf_protect, require_owner, user_shop
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.ratelimit import client_ip, limiter
from app.routers.intake import read_limited_body
from app.services.voice import apply_call_result, verify_signature, voice_allowed

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)


@router.post("/webhooks/voice")
async def voice_webhook(
    request: Request,
    db: Session = Depends(get_db),
    elevenlabs_signature: str | None = Header(default=None),
) -> JSONResponse:
    limiter.check(f"voice-wh:{client_ip(request)}", 120)
    body = await read_limited_body(request)
    secret = get_settings().elevenlabs_webhook_secret.get_secret_value()
    if not verify_signature(secret, body, elevenlabs_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid payload")
    return JSONResponse({"result": apply_call_result(db, payload)})


@router.post("/dashboard/voice", dependencies=[Depends(csrf_protect)])
def save_voice(
    voice_calls_on: str = Form(default=""),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    shop.voice_calls_on = voice_calls_on == "on" and voice_allowed(shop)
    db.commit()
    return RedirectResponse("/dashboard/settings?msg=saved#voice", status_code=303)
