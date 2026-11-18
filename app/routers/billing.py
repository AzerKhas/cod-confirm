"""Subscription pages (shop owner) and payment provider callbacks.

Callbacks (`/billing/<provider>/return|notify`) are public: they only carry a reference,
and the status is always re-checked with the provider's API before anything is applied.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import csrf_protect, require_owner, user_shop
from app.config import get_settings
from app.db import get_db
from app.models import Payment, User
from app.plans import DURATIONS, PAID_PLANS, PLANS, Plan, price_millimes
from app.ratelimit import client_ip, limiter
from app.routers.dashboard import _ctx
from app.services import usage
from app.services.billing import (
    BillingError,
    apply_paid,
    billing_state,
    find_by_ref,
    start_checkout,
    verify_payment,
)
from app.services.payments import PaymentProviderError, available_providers
from app.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)

BILLING_FLASH = {
    "paid": (True, "Paiement confirmé, merci ! Votre abonnement est prolongé."),
    "pending": (True, "Paiement en cours de vérification. Il sera pris en compte automatiquement."),
    "failed": (False, "Le paiement n'a pas abouti. Vous pouvez réessayer."),
    "expired": (False, "Le lien de paiement a expiré. Vous pouvez réessayer."),
    "provider_error": (False, "Le service de paiement ne répond pas. Réessayez dans un instant."),
    "invalid": (False, "Choix invalide."),
}
CALLBACKS_PER_MINUTE = 30


def _base_url(request: Request) -> str:
    return get_settings().public_base_url or str(request.base_url)


def _back(state: str) -> RedirectResponse:
    return RedirectResponse(f"/dashboard/billing?msg={state}", status_code=303)


# ------------------------------------------------------------- owner pages
@router.get("/dashboard/billing", response_class=HTMLResponse)
def billing_page(
    request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = user_shop(db, user)
    payments = db.scalars(
        select(Payment).where(Payment.shop_id == shop.id).order_by(Payment.id.desc()).limit(24)
    ).all()
    used = usage.counter(db, shop.id).orders
    offers = [
        {
            "plan": p,
            "limits": PLANS[p],
            "prices": {m: price_millimes(p, m) // 1000 for m in DURATIONS},
        }
        for p in PAID_PLANS
    ]
    ctx = _ctx(
        request,
        user,
        shop,
        nav="billing",
        payments=payments,
        offers=offers,
        durations=DURATIONS,
        providers=available_providers(),
        state=billing_state(shop),
        current=PLANS[Plan(shop.plan)],
        used=used,
        Plan=Plan,
        plan_labels={p.value: lim.label for p, lim in PLANS.items()},
    )
    ctx["flash_messages"] = {**ctx["flash_messages"], **BILLING_FLASH}
    return templates.TemplateResponse(request, "dashboard/billing.html", ctx)


@router.post("/dashboard/billing/checkout", dependencies=[Depends(csrf_protect)])
def checkout(
    request: Request,
    plan: str = Form(max_length=20),
    months: int = Form(ge=1, le=24),
    provider: str = Form(max_length=20),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    limiter.check(f"checkout:{shop.id}", 10)
    try:
        payment = start_checkout(db, shop, user, plan, months, provider, _base_url(request))
    except BillingError:
        return _back("invalid")
    except PaymentProviderError:
        return _back("provider_error")
    return RedirectResponse(payment.pay_url or "/dashboard/billing", status_code=303)


@router.get("/dashboard/billing/receipts/{payment_id}", response_class=HTMLResponse)
def receipt(
    payment_id: int,
    request: Request,
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = user_shop(db, user)
    payment = db.scalar(
        select(Payment).where(
            Payment.id == payment_id, Payment.shop_id == shop.id, Payment.status == "paid"
        )
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "dashboard/receipt.html",
        {"shop": shop, "payment": payment, "plan": PLANS[Plan(payment.plan)], "user": user},
    )


# ------------------------------------------------------- provider callbacks
def _verify_and_redirect(db: Session, payment: Payment | None) -> RedirectResponse:
    if payment is None:
        return _back("pending")
    state = verify_payment(db, payment)
    return _back(state if state in BILLING_FLASH else "pending")


@router.get("/billing/konnect/notify")
def konnect_notify(
    request: Request,
    payment_ref: str = Query(default="", max_length=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Konnect calls this URL server-side AND redirects the payer here (GET ?payment_ref=)."""
    limiter.check(f"billing-cb:{client_ip(request)}", CALLBACKS_PER_MINUTE)
    return _verify_and_redirect(db, find_by_ref(db, "konnect", payment_ref))


@router.get("/billing/{provider}/return")
def provider_return(
    provider: str,
    request: Request,
    p: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Browser comes back from the payment page (Flouci success_link / fail_link)."""
    limiter.check(f"billing-cb:{client_ip(request)}", CALLBACKS_PER_MINUTE)
    payment = db.get(Payment, p) if p else None
    if payment is not None and payment.provider != provider:
        payment = None
    return _verify_and_redirect(db, payment)


@router.get("/billing/flouci/notify")
def flouci_notify(
    request: Request,
    payment_id: str = Query(default="", max_length=100),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Flouci webhook v1 (GET ?payment_id=&success=): only triggers a verification."""
    limiter.check(f"billing-cb:{client_ip(request)}", CALLBACKS_PER_MINUTE)
    payment = find_by_ref(db, "flouci", payment_id)
    if payment is None:
        return JSONResponse({"status": "ignored"})
    return JSONResponse({"status": verify_payment(db, payment)})


# ------------------------------------------------------------ demo checkout
def _demo_payment(db: Session, user: User, payment_id: int) -> Payment:
    if "demo" not in available_providers():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    shop = user_shop(db, user)
    payment = db.scalar(
        select(Payment).where(
            Payment.id == payment_id, Payment.shop_id == shop.id, Payment.provider == "demo"
        )
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return payment


@router.get("/billing/demo/{payment_id}", response_class=HTMLResponse)
def demo_page(
    payment_id: int,
    request: Request,
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    payment = _demo_payment(db, user, payment_id)
    return templates.TemplateResponse(
        request,
        "dashboard/demo_checkout.html",
        _ctx(
            request,
            user,
            user_shop(db, user),
            payment=payment,
            plan=PLANS[Plan(payment.plan)],
            nav="billing",
        ),
    )


@router.post("/billing/demo/{payment_id}", dependencies=[Depends(csrf_protect)])
def demo_pay(
    payment_id: int,
    result: str = Form(max_length=10),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    payment = _demo_payment(db, user, payment_id)
    if payment.status != "pending":
        return _back(payment.status)
    if result == "success":
        apply_paid(db, payment, note="paiement de démonstration")
        return _back("paid")
    payment.status = "failed"
    db.commit()
    return _back("failed")
