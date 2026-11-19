"""Public sign-up: a seller creates their shop and starts the free trial in one minute.

Protections: per-IP rate limit (5 sign-ups / hour), signed expiring form token, honeypot,
server-side validation, password hashed with scrypt. Can be turned off with SIGNUP_ENABLED.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import login_session
from app.config import get_settings
from app.db import get_db
from app.enums import Language
from app.ratelimit import client_ip, limiter
from app.security import check_form_token, make_form_token
from app.services.onboarding import SignupError, create_account
from app.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)

SIGNUPS_PER_HOUR = 5
LANGUAGES = [(Language.DARIJA, "Tounsi (darija)"), (Language.FRENCH, "Français"),
             (Language.ARABIC, "العربية"), (Language.ENGLISH, "English")]  # fmt: skip


def _enabled() -> None:
    if not get_settings().signup_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def _render(
    request: Request, values: dict[str, str], errors: dict[str, str], code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard/signup.html",
        {"values": values, "errors": errors, "token": make_form_token("signup"),
         "languages": LANGUAGES, "terms_url": get_settings().terms_url,
         "trial_days": get_settings().trial_days},
        status_code=code,
    )  # fmt: skip


@router.get("/signup", response_class=HTMLResponse, dependencies=[Depends(_enabled)])
def signup_page(request: Request) -> HTMLResponse:
    return _render(request, {}, {})


@router.post("/signup", response_class=HTMLResponse, dependencies=[Depends(_enabled)])
async def signup(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    values = {k: str(v)[:300] for k, v in form.items() if isinstance(v, str)}
    if values.get("website"):  # honeypot
        return RedirectResponse("/login", status_code=303)
    if not check_form_token(values.get("ft"), "signup"):
        return _render(request, values, {"_form": "La page a expiré, réessayez."}, 400)
    limiter.check(f"signup:{client_ip(request)}", SIGNUPS_PER_HOUR, 3600)

    terms_url = get_settings().terms_url
    if terms_url and values.get("terms") != "on":
        return _render(request, values, {"terms": "Acceptez les conditions pour continuer."}, 400)
    try:
        language = Language(values.get("language") or Language.DARIJA.value)
    except ValueError:
        language = Language.DARIJA
    try:
        shop, user = create_account(
            db,
            shop_name=values.get("shop_name", ""),
            email=values.get("email", ""),
            password=values.get("password", ""),
            language=language,
            terms_accepted=bool(terms_url),
        )
    except SignupError as exc:
        values.pop("password", None)
        return _render(request, values, {exc.field: str(exc)}, 400)

    login_session(request, user)
    log.info("signup: new shop", extra={"shop_id": shop.id})
    return RedirectResponse("/dashboard/onboarding", status_code=303)
