"""Shop-owner dashboard (Jinja2 + HTMX, self-hosted design system in /static/app.css).

Every query is scoped to the logged-in user's shop: an order of another shop is a 404.
Every POST requires the CSRF token.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.auth import (
    authenticate,
    csrf_protect,
    csrf_token,
    current_shop_user,
    login_session,
    login_throttle,
    logout_session,
    require_owner,
    user_shop,
)
from app.db import get_db
from app.enums import Direction, Language, OrderStatus, Role
from app.models import Customer, MessageLog, Order, Shop, User
from app.ratelimit import client_ip, limiter
from app.schemas import OrderItemIn
from app.services import messages as m
from app.services.billing import billing_state
from app.services.charts import build_chart, daily_outcomes, share_blocks
from app.services.onboarding import onboarding_steps, progress
from app.services.order_admin import ADMIN_ACTIONS, edit_order, resend_confirmation, set_status
from app.services.orders import InvalidTransition, is_risky
from app.services.privacy import delete_customer
from app.services.scheduler import is_quiet_hours
from app.services.sheets import service_account_email
from app.services.shipping import CARRIERS, carrier_label, tracking_url
from app.services.stats import compute_stats, local_day_bounds
from app.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)
S = OrderStatus
PAGE_SIZE = 25

# (label, pill style) - styles are classes of app.css (.pill-<style>).
STATUS_LABELS: dict[OrderStatus, tuple[str, str]] = {
    S.NEW: ("Nouvelle", "new"),
    S.AWAITING_CONFIRMATION: ("En attente", "pending"),
    S.CONFIRMED: ("Confirmée", "good"),
    S.MODIFY_REQUESTED: ("À modifier", "modify"),
    S.CANCELLED: ("Annulée", "bad"),
    S.NO_ANSWER: ("Sans réponse", "warn"),
    S.SHIPPED: ("Expédiée", "info"),
    S.DELIVERED: ("Livrée", "done"),
    S.REFUSED: ("Refusée", "refused"),
}
# flash code -> (success?, message), shown as a toast.
FLASH_MESSAGES: dict[str, tuple[bool, str]] = {
    "status": (True, "Statut mis à jour."),
    "edited": (True, "Commande modifiée."),
    "resent": (True, "Confirmation renvoyée sur WhatsApp."),
    "not_sent": (False, "Message non envoyé (client STOP, quota ou erreur WhatsApp)."),
    "invalid": (False, "Action impossible ou données invalides."),
    "saved": (True, "Réglages enregistrés."),
    "deleted": (True, "Données du client supprimées."),
    "branding_invalid": (
        False,
        "Logo refusé : PNG, JPEG ou WebP de 300 Ko maximum, couleur au format #RRGGBB.",
    ),
}
ACTION_LABELS = {
    S.CONFIRMED: "Confirmer (par téléphone)",
    S.CANCELLED: "Annuler",
    S.SHIPPED: "Marquer expédiée",
    S.DELIVERED: "Marquer livrée",
    S.REFUSED: "Refusée (retour)",
}
PERIODS = {"today": 0, "7d": 6, "30d": 29}
CHART_DAYS = {"today": 14, "7d": 7, "30d": 30}
PERIOD_TEXT = {
    "today": ("aujourd'hui", "Aujourd'hui", "vs hier"),
    "7d": ("les 7 derniers jours", "7 jours", "vs période préc."),
    "30d": ("les 30 derniers jours", "30 jours", "vs période préc."),
}


def _ctx(request: Request, user: User, shop: Shop, **extra: Any) -> dict[str, Any]:
    from sqlalchemy.orm import object_session

    db = object_session(shop)
    todo = 0
    if db is not None:
        todo = (
            db.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.shop_id == shop.id, Order.status == S.MODIFY_REQUESTED)
            )
            or 0
        )
    return {
        "user": user,
        "shop": shop,
        "csrf": csrf_token(request),
        "status_labels": STATUS_LABELS,
        "fmt": m.format_amount,
        "items_summary": m.items_summary,
        "todo_count": todo,
        "billing": billing_state(shop),
        "setup": progress(onboarding_steps(db, shop)) if db is not None else None,
        "flash": request.query_params.get("msg"),
        "flash_messages": FLASH_MESSAGES,
        **extra,
    }


def _get_order(db: Session, shop: Shop, order_id: int) -> Order:
    order = db.scalar(select(Order).where(Order.id == order_id, Order.shop_id == shop.id))
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return order


def _period_bounds(shop: Shop, period: str) -> tuple[datetime, datetime]:
    days = PERIODS.get(period, 0)
    today = datetime.now(UTC).astimezone(_tz(shop)).date()
    start, _ = local_day_bounds(today - timedelta(days=days), shop.timezone)
    _, end = local_day_bounds(today, shop.timezone)
    return start, end


def _tz(shop: Shop) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(shop.timezone or "Africa/Tunis")


# ------------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard/login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(max_length=254),
    password: str = Form(max_length=200),
    db: Session = Depends(get_db),
) -> Response:
    ip = client_ip(request)
    limiter.check(f"login:{ip}", 20)
    if login_throttle.blocked(ip, email):
        return templates.TemplateResponse(
            request, "dashboard/login.html",
            {"error": "Trop de tentatives. Réessayez dans 15 minutes."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )  # fmt: skip
    user = authenticate(db, email, password)
    if user is None:
        login_throttle.fail(ip, email)
        log.info("auth: login failed")
        return templates.TemplateResponse(
            request, "dashboard/login.html", {"error": "Email ou mot de passe incorrect."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )  # fmt: skip
    login_throttle.reset(ip, email)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    login_session(request, user)
    log.info("auth: login ok", extra={"user_id": user.id})
    target = "/admin" if user.role is Role.SUPERADMIN else "/dashboard"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request) -> RedirectResponse:
    logout_session(request)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# ------------------------------------------------------------------- home
def _pts(now: float, before: float, before_total: int) -> float | None:
    """Difference in percentage points, None when there is nothing to compare with."""
    return round(now - before, 1) if before_total else None


@router.get("/dashboard", response_class=HTMLResponse)
def home(
    request: Request,
    period: str = Query(default="today", pattern="^(today|7d|30d)$"),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = user_shop(db, user)
    start, end = _period_bounds(shop, period)
    stats = compute_stats(db, shop, start, end)
    prev = compute_stats(db, shop, start - (end - start), start)
    deltas = {
        "total": stats.total - prev.total if prev.total or stats.total else None,
        "confirmation_rate": _pts(stats.confirmation_rate, prev.confirmation_rate, prev.total),
        "cancellation_rate": _pts(stats.cancellation_rate, prev.cancellation_rate, prev.total),
        "no_answer_rate": _pts(stats.no_answer_rate, prev.no_answer_rate, prev.total),
    }
    today = datetime.now(UTC).astimezone(_tz(shop)).date()
    chart = build_chart(daily_outcomes(db, shop, today, CHART_DAYS[period]))
    shares = share_blocks({"confirmed": stats.confirmed, "cancelled": stats.cancelled,
                           "no_answer": stats.no_answer, "pending": stats.pending})  # fmt: skip
    to_handle = db.scalars(
        select(Order)
        .where(Order.shop_id == shop.id, Order.status.in_((S.MODIFY_REQUESTED, S.NO_ANSWER)))
        .order_by(Order.updated_at.desc())
        .limit(10)
    ).all()
    today_start, today_end = _period_bounds(shop, "today")
    today_orders = db.scalars(
        select(Order)
        .where(Order.shop_id == shop.id, Order.created_at >= today_start,
               Order.created_at < today_end)
        .order_by(Order.id.desc())
        .limit(50)
    ).all()  # fmt: skip
    return templates.TemplateResponse(
        request,
        "dashboard/home.html",
        _ctx(request, user, shop, stats=stats, period=period, to_handle=to_handle,
             today_orders=today_orders, is_risky=is_risky, nav="home", deltas=deltas,
             chart=chart, shares=shares, period_label=PERIOD_TEXT[period][0],
             period_short=PERIOD_TEXT[period][1], compare_label=PERIOD_TEXT[period][2]),
    )  # fmt: skip


# ----------------------------------------------------------------- orders
def _orders_query(shop: Shop, status_filter: str | None, q: str | None) -> Any:
    query = select(Order).join(Customer).where(Order.shop_id == shop.id)
    if status_filter:
        try:
            query = query.where(Order.status == OrderStatus(status_filter))
        except ValueError:
            pass
    if q:
        like = f"%{q.strip()[:60]}%"
        conds = [Customer.name.ilike(like), Customer.phone_e164.like(like), Order.city.ilike(like)]
        if q.strip().isdigit():
            conds.append(Order.id == int(q.strip()))
        query = query.where(or_(*conds))
    return query


@router.get("/dashboard/orders", response_class=HTMLResponse)
def orders_page(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    q: str | None = Query(default=None, max_length=60),
    page: int = Query(default=1, ge=1, le=10_000),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = user_shop(db, user)
    query = _orders_query(shop, status_filter, q)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    orders = db.scalars(
        query.order_by(Order.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).all()
    ctx = _ctx(
        request, user, shop, orders=orders, status_filter=status_filter or "", q=q or "",
        page=page, pages=max(1, -(-total // PAGE_SIZE)), total=total, is_risky=is_risky,
        nav="todo" if status_filter == S.MODIFY_REQUESTED.value else "orders",
    )  # fmt: skip
    # HTMX requests only need the table.
    name = (
        "dashboard/_orders_table.html"
        if request.headers.get("hx-request")
        else "dashboard/orders.html"
    )
    return templates.TemplateResponse(request, name, ctx)


@router.get("/dashboard/orders/{order_id}", response_class=HTMLResponse)
def order_detail(
    order_id: int,
    request: Request,
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    shop = user_shop(db, user)
    order = _get_order(db, shop, order_id)
    history = db.scalars(
        select(MessageLog)
        .where(or_(MessageLog.order_id == order.id,
                   (MessageLog.customer_id == order.customer_id) & MessageLog.order_id.is_(None)))
        .order_by(MessageLog.created_at, MessageLog.id)
    ).all()  # fmt: skip
    return templates.TemplateResponse(
        request,
        "dashboard/order_detail.html",
        _ctx(request, user, shop, order=order, customer=order.customer, history=history,
             actions=[(a, ACTION_LABELS[a]) for a in ADMIN_ACTIONS[order.status]],
             can_resend=order.status in (S.NEW, S.AWAITING_CONFIRMATION, S.NO_ANSWER),
             editable=order.status not in (S.SHIPPED, S.DELIVERED, S.REFUSED, S.CANCELLED),
             risky=is_risky(order.customer), Direction=Direction, nav="orders",
             carriers=CARRIERS, carrier_label=carrier_label, tracking_url=tracking_url,
             flash=request.query_params.get("msg")),
    )  # fmt: skip


def _back(order: Order, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/dashboard/orders/{order.id}?msg={msg}", status_code=303)


@router.post("/dashboard/orders/{order_id}/status", dependencies=[Depends(csrf_protect)])
def change_status(
    order_id: int,
    to: str = Form(max_length=32),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    order = _get_order(db, shop, order_id)
    try:
        target = OrderStatus(to)
        if target not in ADMIN_ACTIONS[order.status]:
            raise InvalidTransition(order.status, target, user.role)  # type: ignore[arg-type]
        set_status(db, order, target, note=f"by {user.email}")
    except (ValueError, InvalidTransition):
        db.rollback()
        return _back(order, "invalid")
    if target is S.SHIPPED:
        from app.services.order_admin import send_shipped_notice

        if not is_quiet_hours(shop, datetime.now(UTC)):
            send_shipped_notice(db, order)
    return _back(order, "status")


@router.post("/dashboard/orders/{order_id}/resend", dependencies=[Depends(csrf_protect)])
def resend(
    order_id: int, user: User = Depends(current_shop_user), db: Session = Depends(get_db)
) -> RedirectResponse:
    order = _get_order(db, user_shop(db, user), order_id)
    return _back(order, "resent" if resend_confirmation(db, order) else "not_sent")


@router.post("/dashboard/orders/{order_id}/edit", dependencies=[Depends(csrf_protect)])
async def edit(
    order_id: int,
    request: Request,
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    order = _get_order(db, user_shop(db, user), order_id)
    form = await request.form()
    names, qtys, prices = (
        form.getlist("item_name"),
        form.getlist("item_qty"),
        form.getlist("item_price"),
    )
    try:
        items = [
            OrderItemIn(name=str(n), qty=int(str(qn)), price=Decimal(str(pr)))
            for n, qn, pr in zip(names, qtys, prices, strict=False)
            if str(n).strip()
        ][:50]
        address = str(form.get("address", "")).strip()[:300]
        city = str(form.get("city", "")).strip()[:80]
        raw_total = str(form.get("total", "")).strip()
        total = Decimal(raw_total).quantize(Decimal("0.001")) if raw_total else None
        if not items or not address or not city or (total is not None and total < 0):
            raise ValueError
        edit_order(db, order, address=address, city=city, items=items, total=total)
    except (ValueError, InvalidOperation, ValidationError):
        db.rollback()
        return _back(order, "invalid")
    return _back(order, "edited")


@router.post("/dashboard/customers/{customer_id}/delete", dependencies=[Depends(csrf_protect)])
def remove_customer(
    customer_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> RedirectResponse:
    shop = user_shop(db, user)
    customer = db.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.shop_id == shop.id)
    )
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    delete_customer(db, customer)
    db.commit()
    return RedirectResponse("/dashboard/orders?msg=deleted", status_code=303)


# --------------------------------------------------------------------- CSV
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> str:
    """Prevent CSV/formula injection when the file is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


@router.get("/dashboard/export.csv")
def export_csv(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    q: str | None = Query(default=None, max_length=60),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    shop = user_shop(db, user)
    query = _orders_query(shop, status_filter, q)
    if date_from:
        query = query.where(Order.created_at >= local_day_bounds(date_from, shop.timezone)[0])
    if date_to:
        query = query.where(Order.created_at < local_day_bounds(date_to, shop.timezone)[1])
    orders = db.scalars(query.order_by(Order.id).limit(50_000)).all()

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel shows Arabic / accents correctly
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["id", "date", "statut", "client", "telephone", "ville", "adresse",
                     "produits", "total", "devise", "source", "relances"])  # fmt: skip
    tz = _tz(shop)
    for o in orders:
        writer.writerow([_csv_safe(v) for v in (
            o.id, o.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            STATUS_LABELS[o.status][0], o.customer.name, o.customer.phone_e164, o.city,
            o.address, m.items_summary(o.items, 500), m.format_amount(o.total), o.currency,
            o.source.value, o.reminders_sent,
        )])  # fmt: skip
    filename = f"commandes-{shop.slug}-{datetime.now(UTC):%Y%m%d}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- settings
@router.get("/dashboard/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = user_shop(db, user)
    products = "\n".join(f"{p['name']} = {p['price']}" for p in shop.form_products or [])
    return templates.TemplateResponse(
        request,
        "dashboard/settings.html",
        _ctx(request, user, shop, products=products, languages=list(Language),
             flash=request.query_params.get("msg"), sa_email=service_account_email(),
             carriers=CARRIERS,
             nav="settings"),
    )  # fmt: skip


@router.post("/dashboard/settings", dependencies=[Depends(csrf_protect)])
def save_settings(
    name: str = Form(min_length=1, max_length=120),
    default_language: str = Form(max_length=10),
    delivery_fee: str = Form(max_length=20),
    reminder_1: int = Form(ge=1, le=72),
    reminder_2: int = Form(ge=1, le=168),
    data_retention_days: int = Form(ge=30, le=3650),
    owner_telegram_chat_id: str = Form(default="", max_length=40),
    products: str = Form(default="", max_length=10_000),
    google_sheet_id: str = Form(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$"),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    try:
        catalog = []
        for line in products.splitlines():
            if not line.strip():
                continue
            pname, _, price = line.rpartition("=")
            if not pname.strip():
                raise ValueError
            amount = Decimal(price.strip()).quantize(Decimal("0.001"))
            if amount < 0:
                raise ValueError
            catalog.append({"name": pname.strip()[:200], "price": str(amount)})
        fee = Decimal(delivery_fee.replace(",", ".")).quantize(Decimal("0.001"))
        if fee < 0 or reminder_2 <= reminder_1:
            raise ValueError
        shop.name = name.strip()
        shop.default_language = Language(default_language)
        shop.delivery_fee = fee
        shop.reminder_delays = [reminder_1, reminder_2]
        shop.data_retention_days = data_retention_days
        shop.owner_telegram_chat_id = owner_telegram_chat_id.strip() or None
        shop.form_products = catalog[:100]
        if (google_sheet_id or None) != shop.google_sheet_id:
            shop.google_sheet_id = google_sheet_id or None
            # New sheet: push every order again.
            db.execute(update(Order).where(Order.shop_id == shop.id).values(sheet_dirty=True))
        db.commit()
    except (ValueError, InvalidOperation):
        db.rollback()
        return RedirectResponse("/dashboard/settings?msg=invalid", status_code=303)
    return RedirectResponse("/dashboard/settings?msg=saved", status_code=303)
