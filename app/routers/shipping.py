"""Shipments page (dashboard) and the signed carrier status webhook."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import csrf_protect, current_shop_user, require_owner, user_shop
from app.crypto import encrypt_secret, reveal_secret
from app.db import get_db
from app.models import Order, Shop, User
from app.ratelimit import client_ip, limiter
from app.routers.dashboard import _ctx, _get_order
from app.routers.intake import read_limited_body
from app.security import verify_base64_hmac
from app.services import messages as m
from app.services.order_admin import send_shipped_notice
from app.services.orders import InvalidTransition
from app.services.scheduler import is_quiet_hours
from app.services.shipping import (
    CARRIERS,
    apply_carrier_status,
    carrier_export_csv,
    carrier_label,
    find_order,
    import_status_csv,
    in_transit,
    map_carrier_status,
    ready_to_ship,
    selected_orders,
    ship_many,
    ship_order,
    tracking_url,
)
from app.templating import templates

router = APIRouter(include_in_schema=False)
log = logging.getLogger(__name__)

SHIPPING_FLASH = {
    "shipped": (True, "Commandes marquées expédiées. Les clients sont prévenus sur WhatsApp."),
    "none": (False, "Aucune commande confirmée sélectionnée."),
    "carrier": (False, "Choisissez un transporteur."),
    "saved": (True, "Réglages de livraison enregistrés."),
}


def _send_notices(db: Session, shop: Shop, orders: list[Order]) -> None:
    if is_quiet_hours(shop, datetime.now(UTC)):
        return  # the scheduler sends them after the quiet hours
    for order in orders:
        send_shipped_notice(db, order)


def _ids(raw: list[str]) -> list[int]:
    return [int(x) for x in raw if x.isdigit()][:500]


@router.get("/dashboard/shipping", response_class=HTMLResponse)
def shipping_page(
    request: Request, user: User = Depends(current_shop_user), db: Session = Depends(get_db)
) -> HTMLResponse:
    shop = user_shop(db, user)
    ready, transit = ready_to_ship(db, shop), in_transit(db, shop)
    ctx = _ctx(request, user, shop, nav="shipping", ready=ready, transit=transit, carriers=CARRIERS,
               carrier_label=carrier_label, tracking_url=tracking_url,
               report=request.session.pop("import_report", None))  # fmt: skip
    ctx["flash_messages"] = {**ctx["flash_messages"], **SHIPPING_FLASH}
    return templates.TemplateResponse(request, "dashboard/shipping.html", ctx)


@router.post("/dashboard/shipping/ship", dependencies=[Depends(csrf_protect)])
async def ship_selected(
    request: Request, user: User = Depends(current_shop_user), db: Session = Depends(get_db)
) -> RedirectResponse:
    shop = user_shop(db, user)
    form = await request.form()
    carrier = str(form.get("carrier", ""))
    if carrier not in CARRIERS:
        return RedirectResponse("/dashboard/shipping?msg=carrier", status_code=303)
    ids = _ids([str(v) for v in form.getlist("ids")])
    shipped = ship_many(db, shop, ids, carrier, by=user.email) if ids else []
    if not shipped:
        return RedirectResponse("/dashboard/shipping?msg=none", status_code=303)
    _send_notices(db, shop, shipped)
    return RedirectResponse("/dashboard/shipping?msg=shipped", status_code=303)


@router.post("/dashboard/orders/{order_id}/ship", dependencies=[Depends(csrf_protect)])
def ship_one(
    order_id: int,
    carrier: str = Form(max_length=30),
    tracking_number: str = Form(default="", max_length=64),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    order = _get_order(db, shop, order_id)
    try:
        ship_order(db, order, carrier, tracking_number, by=user.email)
        db.commit()
    except (ValueError, InvalidTransition):
        db.rollback()
        return RedirectResponse(f"/dashboard/orders/{order.id}?msg=invalid", status_code=303)
    _send_notices(db, shop, [order])
    return RedirectResponse(f"/dashboard/orders/{order.id}?msg=status", status_code=303)


def _printable(
    request: Request, user: User, db: Session, ids: list[int], template: str
) -> HTMLResponse:
    shop = user_shop(db, user)
    orders = selected_orders(db, shop, ids)
    total = sum((o.total for o in orders), start=Decimal(0))
    return templates.TemplateResponse(
        request, template,
        {"shop": shop, "orders": orders, "total": total, "fmt": m.format_amount,
         "items_summary": m.items_summary, "carrier_label": carrier_label,
         "now": datetime.now(UTC)},
    )  # fmt: skip


@router.get("/dashboard/shipping/manifest", response_class=HTMLResponse)
def manifest(
    request: Request, ids: list[str] = Query(default=[]), user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:  # fmt: skip
    return _printable(request, user, db, _ids(ids), "dashboard/manifest.html")


@router.get("/dashboard/shipping/labels", response_class=HTMLResponse)
def labels(
    request: Request, ids: list[str] = Query(default=[]), user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:  # fmt: skip
    return _printable(request, user, db, _ids(ids), "dashboard/labels.html")


@router.get("/dashboard/shipping/export.csv")
def export(
    ids: list[str] = Query(default=[]),
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> Response:
    shop = user_shop(db, user)
    body = carrier_export_csv(selected_orders(db, shop, _ids(ids)))
    name = f"colis-{shop.slug}-{datetime.now(UTC):%Y%m%d-%H%M}.csv"
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip


@router.post("/dashboard/shipping/import", dependencies=[Depends(csrf_protect)])
async def import_statuses(
    request: Request,
    file: UploadFile,
    user: User = Depends(current_shop_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    data = await file.read(2_000_001)
    report = import_status_csv(db, shop, data)
    request.session["import_report"] = {
        "delivered": report.delivered, "refused": report.refused, "unchanged": report.unchanged,
        "not_found": report.not_found[:10], "not_found_count": len(report.not_found),
        "error": report.error,
    }  # fmt: skip
    return RedirectResponse("/dashboard/shipping#import", status_code=303)


@router.post("/dashboard/shipping/settings", dependencies=[Depends(csrf_protect)])
def shipping_settings(
    default_carrier: str = Form(default="", max_length=30),
    tracking_url_pattern: str = Form(default="", max_length=300),
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    shop = user_shop(db, user)
    pattern = tracking_url_pattern.strip()
    if pattern and (not pattern.startswith("https://") or "{tracking}" not in pattern):
        return RedirectResponse("/dashboard/settings?msg=invalid#shipping", status_code=303)
    shop.default_carrier = default_carrier if default_carrier in CARRIERS else None
    shop.tracking_url_pattern = pattern or None
    db.commit()
    return RedirectResponse("/dashboard/settings?msg=saved#shipping", status_code=303)


@router.post(
    "/dashboard/shipping/webhook-secret",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
def new_webhook_secret(
    request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)
) -> HTMLResponse:
    """Secret used by a carrier / integrator to sign status updates. Shown once."""
    shop = user_shop(db, user)
    secret = "whsec_" + secrets.token_urlsafe(32)
    shop.carrier_webhook_secret = encrypt_secret(secret)
    db.commit()
    return templates.TemplateResponse(
        request,
        "dashboard/webhook_secret_once.html",
        _ctx(request, user, shop, secret=secret, nav="settings"),
    )


# ------------------------------------------------------------ carrier webhook
@router.post("/webhooks/carrier/{shop_slug}")
async def carrier_webhook(
    shop_slug: str,
    request: Request,
    db: Session = Depends(get_db),
    x_signature: str | None = Header(default=None),
) -> JSONResponse:
    """Status updates pushed by a carrier or an integrator.

    Body: {"updates": [{"tracking_number" | "reference": "...", "status": "delivered"}]}
    Header `X-Signature`: base64(HMAC-SHA256(secret, raw body)).
    """
    limiter.check(f"carrier-wh:{client_ip(request)}", 120)
    body = await read_limited_body(request)
    shop = db.scalar(select(Shop).where(Shop.slug == shop_slug)) if len(shop_slug) <= 60 else None
    secret = reveal_secret(shop.carrier_webhook_secret) if shop else None
    if shop is None or not verify_base64_hmac(secret, body, x_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    try:
        payload = json.loads(body)
        updates = payload.get("updates", [payload]) if isinstance(payload, dict) else []
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc

    result = {"updated": 0, "ignored": 0, "not_found": 0}
    for u in updates[:500] if isinstance(updates, list) else []:
        if not isinstance(u, dict):
            continue
        ref = str(u.get("tracking_number") or u.get("reference") or "")
        order = find_order(db, shop, ref)
        if order is None:
            result["not_found"] += 1
            continue
        new_status = map_carrier_status(str(u.get("status", "")))
        if new_status is not None and apply_carrier_status(db, order, new_status, "webhook"):
            result["updated"] += 1
        else:
            result["ignored"] += 1
    db.commit()
    return JSONResponse(result)
