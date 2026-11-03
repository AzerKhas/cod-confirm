"""Super-admin pages (COD Confirm operator): shops, plans, usage, WhatsApp connection.

Secrets (API keys, initial passwords) are shown ONCE right after creation and never stored
in plain text. Every POST requires the CSRF token and the superadmin role.
"""

from __future__ import annotations

import re
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import csrf_protect, csrf_token, hash_password, require_superadmin
from app.crypto import encrypt_secret
from app.db import get_db
from app.enums import Language, OrderStatus, Role
from app.models import Order, Shop, UsageCounter, User
from app.plans import PLANS, Plan, hard_quota
from app.security import generate_api_key
from app.services.usage import month_key
from app.templating import templates

router = APIRouter(prefix="/admin", include_in_schema=False)
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])$")


def _ctx(request: Request, user: User, **extra: Any) -> dict[str, Any]:
    return {"user": user, "csrf": csrf_token(request), "plans": PLANS, "Plan": Plan, **extra}


def _shop(db: Session, shop_id: int) -> Shop:
    shop = db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return shop


def _usage_map(db: Session, month: str) -> dict[int, UsageCounter]:
    return {
        u.shop_id: u for u in db.scalars(select(UsageCounter).where(UsageCounter.month == month))
    }


@router.get("", response_class=HTMLResponse)
def shops_list(
    request: Request, user: User = Depends(require_superadmin), db: Session = Depends(get_db)
) -> HTMLResponse:
    month = month_key()
    shops = db.scalars(select(Shop).order_by(Shop.id.desc())).all()
    usage = _usage_map(db, month)
    totals = {
        "shops": len(shops),
        "active": sum(s.is_active for s in shops),
        "orders": sum(u.orders for u in usage.values()),
        "messages": sum(u.messages_sent for u in usage.values()),
        "mrr": sum(PLANS[Plan(s.plan)].price_tnd for s in shops if s.is_active),
    }
    return templates.TemplateResponse(
        request, "admin/shops.html",
        _ctx(request, user, shops=shops, usage=usage, month=month, totals=totals,
             hard_quota=hard_quota),
    )  # fmt: skip


@router.get("/shops/new", response_class=HTMLResponse)
def new_shop_page(request: Request, user: User = Depends(require_superadmin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin/shop_new.html", _ctx(request, user, error=None, languages=list(Language))
    )


@router.post("/shops/new", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def create_shop(
    request: Request,
    name: str = Form(min_length=1, max_length=120),
    slug: str = Form(min_length=3, max_length=60),
    plan: str = Form(max_length=20),
    default_language: str = Form(max_length=10),
    owner_email: str = Form(min_length=3, max_length=254),
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    slug = slug.strip().lower()
    email = owner_email.strip().lower()
    error = None
    if not SLUG_RE.match(slug):
        error = "Slug invalide (lettres minuscules, chiffres et tirets)."
    elif db.scalar(select(Shop.id).where(Shop.slug == slug)):
        error = "Ce slug existe déjà."
    elif "@" not in email or db.scalar(select(User.id).where(User.email == email)):
        error = "Email invalide ou déjà utilisé."
    try:
        plan_v, lang_v = Plan(plan), Language(default_language)
    except ValueError:
        error = error or "Plan ou langue invalide."
    if error:
        return templates.TemplateResponse(
            request, "admin/shop_new.html",
            _ctx(request, user, error=error, languages=list(Language)), status_code=400,
        )  # fmt: skip

    api_key, key_hash, prefix = generate_api_key()
    password = secrets.token_urlsafe(12)
    shop = Shop(name=name.strip(), slug=slug, plan=plan_v.value, default_language=lang_v,
                api_key_hash=key_hash, api_key_prefix=prefix)  # fmt: skip
    db.add(shop)
    db.flush()
    db.add(
        User(email=email, password_hash=hash_password(password), role=Role.OWNER, shop_id=shop.id)
    )
    db.commit()
    return templates.TemplateResponse(
        request, "admin/secrets_once.html",
        _ctx(request, user, shop=shop, api_key=api_key, owner_email=email, password=password),
    )  # fmt: skip


@router.get("/shops/{shop_id}", response_class=HTMLResponse)
def shop_detail(
    shop_id: int,
    request: Request,
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = _shop(db, shop_id)
    history = db.scalars(
        select(UsageCounter)
        .where(UsageCounter.shop_id == shop.id)
        .order_by(UsageCounter.month.desc())
        .limit(12)
    ).all()
    by_status = dict(
        db.execute(
            select(Order.status, func.count())
            .where(Order.shop_id == shop.id)
            .group_by(Order.status)
        ).all()
    )
    users = db.scalars(select(User).where(User.shop_id == shop.id)).all()
    return templates.TemplateResponse(
        request, "admin/shop_detail.html",
        _ctx(request, user, shop=shop, history=history, by_status=by_status, users=users,
             statuses=list(OrderStatus), hard_quota=hard_quota(shop.plan),
             connected=bool(shop.whatsapp_phone_number_id and shop.access_token_encrypted),
             flash=request.query_params.get("msg")),
    )  # fmt: skip


def _back(shop: Shop, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/shops/{shop.id}?msg={msg}", status_code=303)


@router.post("/shops/{shop_id}/subscription", dependencies=[Depends(csrf_protect)])
def update_subscription(
    shop_id: int,
    plan: str = Form(max_length=20),
    is_active: str = Form(default=""),
    paid_until: str = Form(default="", max_length=10),
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = _shop(db, shop_id)
    try:
        shop.plan = Plan(plan).value
        shop.paid_until = date.fromisoformat(paid_until) if paid_until else None
    except ValueError:
        return _back(shop, "invalid")
    shop.is_active = is_active == "on"
    db.commit()
    return _back(shop, "saved")


@router.post("/shops/{shop_id}/whatsapp", dependencies=[Depends(csrf_protect)])
def update_whatsapp(
    shop_id: int,
    phone_number_id: str = Form(default="", max_length=40, pattern=r"^\d*$"),
    waba_id: str = Form(default="", max_length=40, pattern=r"^\d*$"),
    access_token: str = Form(default="", max_length=1000),
    shopify_secret: str = Form(default="", max_length=200),
    woocommerce_secret: str = Form(default="", max_length=200),
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = _shop(db, shop_id)
    shop.whatsapp_phone_number_id = phone_number_id or None
    shop.waba_id = waba_id or None
    # Empty secret fields mean "keep the current value" (they are never displayed).
    if access_token.strip():
        shop.access_token_encrypted = encrypt_secret(access_token.strip())
    if shopify_secret.strip():
        shop.shopify_webhook_secret = encrypt_secret(shopify_secret.strip())
    if woocommerce_secret.strip():
        shop.woocommerce_webhook_secret = encrypt_secret(woocommerce_secret.strip())
    db.commit()
    return _back(shop, "saved")


@router.post(
    "/shops/{shop_id}/api-key", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)]
)
def regenerate_api_key(
    shop_id: int,
    request: Request,
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = _shop(db, shop_id)
    api_key, shop.api_key_hash, shop.api_key_prefix = generate_api_key()
    db.commit()
    return templates.TemplateResponse(
        request, "admin/secrets_once.html",
        _ctx(request, user, shop=shop, api_key=api_key, owner_email=None, password=None),
    )  # fmt: skip


@router.post(
    "/shops/{shop_id}/users", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)]
)
def add_user(
    shop_id: int,
    request: Request,
    email: str = Form(min_length=3, max_length=254),
    role: str = Form(max_length=10),
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    shop = _shop(db, shop_id)
    email = email.strip().lower()
    if role not in (Role.OWNER.value, Role.STAFF.value) or "@" not in email:
        return _back(shop, "invalid")
    if db.scalar(select(User.id).where(User.email == email)):
        return _back(shop, "exists")
    password = secrets.token_urlsafe(12)
    db.add(
        User(email=email, password_hash=hash_password(password), role=Role(role), shop_id=shop.id)
    )
    db.commit()
    return templates.TemplateResponse(
        request, "admin/secrets_once.html",
        _ctx(request, user, shop=shop, api_key=None, owner_email=email, password=password),
    )  # fmt: skip


@router.post(
    "/users/{user_id}/reset", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)]
)
def reset_password(
    user_id: int,
    request: Request,
    user: User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    target = db.get(User, user_id)
    if target is None or target.role is Role.SUPERADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    password = secrets.token_urlsafe(12)
    target.password_hash = hash_password(password)
    target.session_version += 1  # log out everywhere
    db.commit()
    return templates.TemplateResponse(
        request, "admin/secrets_once.html",
        _ctx(request, user, shop=target.shop, api_key=None, owner_email=target.email,
             password=password),
    )  # fmt: skip
