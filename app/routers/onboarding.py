"""Getting-started checklist and the self-service setup actions of a shop owner:
connect WhatsApp (+ test message), integrations (API key, Shopify / WooCommerce secrets),
and a test order sent to the owner's own phone.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import csrf_protect, require_owner, user_shop
from app.config import get_settings
from app.crypto import encrypt_secret
from app.db import get_db
from app.enums import OrderSource
from app.models import User
from app.ratelimit import limiter
from app.routers.dashboard import _ctx
from app.schemas import OrderIn
from app.security import generate_api_key
from app.services.intake import create_order
from app.services.notify import send_order_confirmation
from app.services.onboarding import onboarding_steps, progress
from app.services.phone import InvalidPhone, normalize_phone
from app.services.whatsapp import WhatsAppError, client_for_shop
from app.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)

ONBOARDING_FLASH = {
    "wa_saved": (True, "WhatsApp connecté. Envoyez-vous un message test."),
    "wa_test_ok": (True, "Message test envoyé : vérifiez votre WhatsApp."),
    "wa_test_err": (False, "Échec de l'envoi : vérifiez le Phone number ID et le token."),
    "wa_invalid": (False, "Phone number ID (chiffres) et token sont obligatoires."),
    "phone_invalid": (False, "Numéro tunisien invalide (8 chiffres, mobile)."),
    "test_ok": (True, "Commande test créée : le message de confirmation part sur votre WhatsApp."),
    "test_not_sent": (
        False,
        "Commande test créée, mais le message n'est pas parti (WhatsApp non connecté ?).",
    ),
    "integrations": (True, "Intégrations enregistrées."),
}


def _back(msg: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/dashboard/onboarding?msg={msg}{anchor}", status_code=303)


@router.get("/dashboard/onboarding", response_class=HTMLResponse)
def onboarding_page(
    request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = user_shop(db, user)
    steps = onboarding_steps(db, shop)
    done, total = progress(steps)
    base = (get_settings().public_base_url or str(request.base_url)).rstrip("/")
    ctx = _ctx(request, user, shop, nav="onboarding", steps={s.key: s for s in steps},
               done=done, total=total, base=base)  # fmt: skip
    ctx["flash_messages"] = {**ctx["flash_messages"], **ONBOARDING_FLASH}
    return templates.TemplateResponse(request, "dashboard/onboarding.html", ctx)


@router.post("/dashboard/onboarding/whatsapp", dependencies=[Depends(csrf_protect)])
def connect_whatsapp(
    phone_number_id: str = Form(default="", max_length=40),
    waba_id: str = Form(default="", max_length=40),
    access_token: str = Form(default="", max_length=1000),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    pnid, waba, token = phone_number_id.strip(), waba_id.strip(), access_token.strip()
    if (
        not pnid.isdigit()
        or (waba and not waba.isdigit())
        or (not token and not shop.access_token_encrypted)
    ):
        return _back("wa_invalid", "#whatsapp")
    shop.whatsapp_phone_number_id = pnid
    shop.waba_id = waba or None
    if token:  # empty field = keep the stored (encrypted) token
        shop.access_token_encrypted = encrypt_secret(token)
    db.commit()
    return _back("wa_saved", "#whatsapp")


@router.post("/dashboard/onboarding/whatsapp/test", dependencies=[Depends(csrf_protect)])
def whatsapp_test(
    phone: str = Form(max_length=32),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Send Meta's pre-approved `hello_world` template (exists in every WhatsApp account)."""
    shop = user_shop(db, user)
    limiter.check(f"wa-test:{shop.id}", 5, 3600)
    try:
        to = normalize_phone(phone)
    except InvalidPhone:
        return _back("phone_invalid", "#whatsapp")
    try:
        client_for_shop(shop).send_template(to, "hello_world", "en_US")
    except WhatsAppError:
        log.warning("onboarding: WhatsApp test failed", extra={"shop_id": shop.id})
        return _back("wa_test_err", "#whatsapp")
    return _back("wa_test_ok", "#whatsapp")


@router.post("/dashboard/onboarding/test-order", dependencies=[Depends(csrf_protect)])
def test_order(
    phone: str = Form(max_length=32),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """A real order to the owner's own phone, to see exactly what customers receive."""
    shop = user_shop(db, user)
    limiter.check(f"test-order:{shop.id}", 10, 3600)
    product = (shop.form_products or [{"name": "Produit test", "price": "0"}])[0]
    try:
        item = {"name": product["name"], "qty": 1, "price": Decimal(str(product.get("price", "0")))}
        data = OrderIn(
            customer_name="Commande test",
            phone=phone,
            address="Adresse de test",
            city="Tunis",
            items=[item],
        )
    except ValidationError:
        return _back("phone_invalid", "#test")
    order = create_order(db, shop, data, OrderSource.API).order
    msg = send_order_confirmation(db, order)
    sent = msg is not None and msg.status.value == "sent"
    return _back("test_ok" if sent else "test_not_sent", "#test")


@router.post("/dashboard/integrations", dependencies=[Depends(csrf_protect)])
def save_integrations(
    shopify_secret: str = Form(default="", max_length=200),
    woocommerce_secret: str = Form(default="", max_length=200),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    if shopify_secret.strip():
        shop.shopify_webhook_secret = encrypt_secret(shopify_secret.strip())
    if woocommerce_secret.strip():
        shop.woocommerce_webhook_secret = encrypt_secret(woocommerce_secret.strip())
    db.commit()
    return _back("integrations", "#orders")


@router.post(
    "/dashboard/api-key", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)]
)
def new_api_key(
    request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> HTMLResponse:
    """New intake API key, shown once (the previous key stops working)."""
    shop = user_shop(db, user)
    api_key, shop.api_key_hash, shop.api_key_prefix = generate_api_key()
    db.commit()
    return templates.TemplateResponse(
        request,
        "dashboard/api_key_once.html",
        _ctx(request, user, shop, api_key=api_key, nav="onboarding"),
    )
