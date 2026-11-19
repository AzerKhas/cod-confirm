"""Hosted order form for Facebook / Instagram shops: GET/POST /form/{shop_slug}.

Post/Redirect/Get: a successful POST redirects to /form/{slug}/done, so refreshing
the page never creates the same order twice. CSRF tokens are added in Phase 9.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import Language, OrderSource
from app.models import Shop
from app.ratelimit import client_ip, limiter
from app.schemas import OrderIn
from app.security import check_form_token, make_form_token
from app.services.branding import LOGO_TYPES, theme_css
from app.services.intake import create_order
from app.services.messages import FORM_LANGUAGES, TN_GOVERNORATES, form_texts
from app.services.notify import send_order_confirmation_job
from app.templating import templates

router = APIRouter(tags=["form"], include_in_schema=False)
FORM_POSTS_PER_MINUTE = 10  # per IP
log = logging.getLogger(__name__)

# OrderIn field -> form field shown to the customer
_FIELD_MAP = {"customer_name": "name", "phone": "phone", "address": "address", "city": "city",
              "items": "product"}  # fmt: skip


def _get_shop(db: Session, slug: str) -> Shop:
    shop = db.scalar(select(Shop).where(Shop.slug == slug)) if len(slug) <= 60 else None
    if shop is None or not shop.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return shop


# The public form opens in French (the most shared written language for online shopping in
# Tunisia); customers switch to Arabic, English or Tunisian with one tap. The language they
# order in is the language of their WhatsApp messages afterwards.
FORM_DEFAULT_LANGUAGE = Language.FRENCH


def _language(shop: Shop, lang: str | None) -> Language:
    try:
        return Language(lang) if lang else FORM_DEFAULT_LANGUAGE
    except ValueError:
        return FORM_DEFAULT_LANGUAGE


def _render(
    request: Request,
    shop: Shop,
    lang: Language,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "form/order_form.html",
        {
            "shop": shop,
            "t": form_texts(lang),
            "lang": lang.value,
            "html_lang": lang.value if lang is not Language.DARIJA else "fr-TN",
            "form_languages": FORM_LANGUAGES,
            "rtl": lang is Language.ARABIC,
            "governorates": TN_GOVERNORATES,
            "products": shop.form_products or [],
            "values": values or {},
            "errors": errors or {},
            "form_token": make_form_token(shop.slug),
        },
        status_code=status_code,
    )


@router.get("/form/{shop_slug}", response_class=HTMLResponse)
def show_form(
    shop_slug: str, request: Request, lang: str | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = _get_shop(db, shop_slug)
    return _render(request, shop, _language(shop, lang))


@router.get("/form/{shop_slug}/theme.css")
def shop_theme(shop_slug: str, db: Session = Depends(get_db)) -> Response:
    """The shop's accent colour as CSS variables (inline styles are forbidden by the CSP)."""
    shop = _get_shop(db, shop_slug)
    css = theme_css(shop.brand_color) if shop.brand_color else ""
    return Response(css, media_type="text/css", headers={"Cache-Control": "public, max-age=300"})


@router.get("/form/{shop_slug}/logo")
def shop_logo(shop_slug: str, db: Session = Depends(get_db)) -> Response:
    shop = _get_shop(db, shop_slug)
    if not shop.logo or shop.logo_mime not in LOGO_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(
        shop.logo,
        media_type=shop.logo_mime,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/form/{shop_slug}/done", response_class=HTMLResponse)
def form_done(
    shop_slug: str, request: Request, lang: str | None = None, db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = _get_shop(db, shop_slug)
    language = _language(shop, lang)
    return templates.TemplateResponse(
        request,
        "form/order_done.html",
        {"shop": shop, "t": form_texts(language), "rtl": language is Language.ARABIC,
         "html_lang": language.value},
    )  # fmt: skip


def _build_item(
    shop: Shop, values: dict[str, str], t: dict[str, str]
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Return (item, errors). The price comes from the shop, never from the form."""
    try:
        qty = int(values.get("qty") or "1")
    except ValueError:
        qty = 0
    if not 1 <= qty <= 20:
        return None, {"qty": t["err_generic"]}

    products = shop.form_products or []
    if products:
        idx = values.get("product", "")
        if not idx.isdigit() or int(idx) >= len(products):
            return None, {"product": t["err_product"]}
        product = products[int(idx)]
        try:
            price = Decimal(str(product.get("price", "0")))
        except InvalidOperation:
            price = Decimal(0)
        return {"name": product["name"], "qty": qty, "price": price}, {}

    name = values.get("product_text", "").strip()
    if not name:
        return None, {"product": t["err_required"]}
    return {"name": name, "qty": qty, "price": Decimal(0)}, {}  # price confirmed by the shop


@router.post("/form/{shop_slug}", response_class=HTMLResponse)
async def submit_form(
    shop_slug: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    limiter.check(f"form:{client_ip(request)}", FORM_POSTS_PER_MINUTE)
    shop = _get_shop(db, shop_slug)
    form = await request.form()
    values = {k: str(v)[:400] for k, v in form.items() if isinstance(v, str)}
    lang = _language(shop, values.get("lang"))
    t = form_texts(lang)
    done_url = f"/form/{shop.slug}/done?lang={lang.value}"

    if not check_form_token(values.get("ft"), shop.slug):
        log.info("form: missing or expired token", extra={"shop_id": shop.id})
        return _render(request, shop, lang, values, {"_form": t["err_generic"]}, 400)

    if values.get("website"):  # honeypot filled -> bot; pretend success, store nothing
        log.info("form: honeypot triggered", extra={"shop_id": shop.id})
        return RedirectResponse(done_url, status_code=status.HTTP_303_SEE_OTHER)

    errors: dict[str, str] = {}
    for field in ("name", "phone", "address"):
        if not values.get(field, "").strip():
            errors[field] = t["err_required"]
    if values.get("city") not in TN_GOVERNORATES:
        errors["city"] = t["err_required"]
    item, item_errors = _build_item(shop, values, t)
    errors.update(item_errors)

    data: OrderIn | None = None
    if not errors and item is not None:
        try:
            data = OrderIn(
                customer_name=values["name"],
                phone=values["phone"],
                address=values["address"],
                city=values["city"],
                items=[item],
                language=lang,
            )
        except ValidationError as exc:
            for err in exc.errors():
                field = _FIELD_MAP.get(str(err["loc"][0]), "_form")
                errors[field] = t["err_phone"] if field == "phone" else t["err_generic"]

    if errors or data is None:
        return _render(request, shop, lang, values, errors, status.HTTP_400_BAD_REQUEST)

    result = create_order(db, shop, data, OrderSource.FORM)
    if result.created:
        background.add_task(send_order_confirmation_job, result.order.id)
    return RedirectResponse(done_url, status_code=status.HTTP_303_SEE_OTHER)
