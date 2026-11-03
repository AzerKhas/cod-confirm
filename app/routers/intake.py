"""Order intake endpoints (JSON): generic API, Shopify webhook, WooCommerce webhook.

The hosted HTML form lives in `routers/order_form.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import reveal_secret
from app.db import get_db
from app.enums import OrderSource
from app.models import Shop
from app.plans import limits
from app.ratelimit import limiter
from app.schemas import OrderCreatedOut, OrderIn, WebhookResultOut
from app.security import hash_api_key, verify_base64_hmac
from app.services.intake import (
    IntakeResult,
    SkipOrder,
    create_order,
    order_in_from_shopify,
    order_in_from_woocommerce,
)
from app.services.notify import send_order_confirmation_job
from app.services.orders import is_risky

router = APIRouter(tags=["intake"])
log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000  # 1 MB is far above any real order payload


# ------------------------------------------------------------------ helpers
def get_shop_by_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Shop:
    if not x_api_key or len(x_api_key) > 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key")
    shop = db.scalar(select(Shop).where(Shop.api_key_hash == hash_api_key(x_api_key)))
    if shop is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key")
    if not shop.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "shop suspended")
    limiter.check(f"api:{shop.id}", limits(shop.plan).api_requests_per_minute)
    return shop


async def read_limited_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload too large")
    return body


def _shop_by_slug(db: Session, slug: str) -> Shop | None:
    if len(slug) > 60:
        return None
    return db.scalar(select(Shop).where(Shop.slug == slug))


def _parse_json(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON object expected")
    return payload


def _validation_reason(exc: ValidationError) -> str:
    fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
    return "invalid fields: " + ", ".join(fields)


def _to_webhook_result(result: IntakeResult) -> WebhookResultOut:
    return WebhookResultOut(
        status="created" if result.created else "duplicate", order_id=result.order.id
    )


def _process_webhook(
    db: Session, shop: Shop, payload: dict[str, Any], source: OrderSource
) -> WebhookResultOut:
    """Map + create. Data problems return 200 'rejected' so the platform does not retry
    forever (Shopify disables a webhook after repeated failures)."""
    if not shop.is_active:
        return WebhookResultOut(status="ignored", reason="shop suspended")
    mapper = order_in_from_shopify if source is OrderSource.SHOPIFY else order_in_from_woocommerce
    try:
        data = mapper(payload)
    except SkipOrder as exc:
        return WebhookResultOut(status="ignored", reason=str(exc))
    except ValidationError as exc:
        reason = _validation_reason(exc)
        log.warning(
            "intake: webhook order rejected",
            extra={"shop_id": shop.id, "source": source.value, "reason": reason},
        )
        return WebhookResultOut(status="rejected", reason=reason)
    return _to_webhook_result(create_order(db, shop, data, source))


def _schedule_confirmation(background: BackgroundTasks, result: WebhookResultOut) -> None:
    if result.status == "created" and result.order_id is not None:
        background.add_task(send_order_confirmation_job, result.order_id)


# ---------------------------------------------------------------- generic API
@router.post(
    "/api/orders",
    response_model=OrderCreatedOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Duplicate external_id, existing order returned"}},
)
def post_order(
    data: OrderIn,
    response: Response,
    background: BackgroundTasks,
    shop: Shop = Depends(get_shop_by_api_key),
    db: Session = Depends(get_db),
) -> OrderCreatedOut:
    """Create an order. Auth: header `X-API-Key: cc_...` (one key per shop)."""
    result = create_order(db, shop, data, OrderSource.API)
    if result.created:
        background.add_task(send_order_confirmation_job, result.order.id)
    else:
        response.status_code = status.HTTP_200_OK
    order = result.order
    return OrderCreatedOut(
        id=order.id,
        status=order.status,
        created=result.created,
        customer_id=order.customer_id,
        risk_warning=is_risky(order.customer),
    )


# ------------------------------------------------------------------- Shopify
@router.post("/webhooks/shopify/{shop_slug}", response_model=WebhookResultOut)
async def shopify_webhook(
    shop_slug: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
) -> WebhookResultOut:
    body = await read_limited_body(request)
    shop = await run_in_threadpool(_shop_by_slug, db, shop_slug)
    secret = reveal_secret(shop.shopify_webhook_secret) if shop else None
    if shop is None or not verify_base64_hmac(secret, body, x_shopify_hmac_sha256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    if x_shopify_topic != "orders/create":
        return WebhookResultOut(status="ignored", reason=f"topic={x_shopify_topic}")
    payload = _parse_json(body)
    result = await run_in_threadpool(_process_webhook, db, shop, payload, OrderSource.SHOPIFY)
    _schedule_confirmation(background, result)
    return result


# --------------------------------------------------------------- WooCommerce
@router.post("/webhooks/woocommerce/{shop_slug}", response_model=WebhookResultOut)
async def woocommerce_webhook(
    shop_slug: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    x_wc_webhook_signature: str | None = Header(default=None),
    x_wc_webhook_topic: str | None = Header(default=None),
) -> WebhookResultOut:
    body = await read_limited_body(request)

    # WooCommerce sends `webhook_id=<n>` (form-encoded) once when the webhook is saved.
    if body.startswith(b"webhook_id=") and len(body) < 64:
        return WebhookResultOut(status="ping")

    shop = await run_in_threadpool(_shop_by_slug, db, shop_slug)
    secret = reveal_secret(shop.woocommerce_webhook_secret) if shop else None
    if shop is None or not verify_base64_hmac(secret, body, x_wc_webhook_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    if x_wc_webhook_topic != "order.created":
        return WebhookResultOut(status="ignored", reason=f"topic={x_wc_webhook_topic}")
    payload = _parse_json(body)
    result = await run_in_threadpool(_process_webhook, db, shop, payload, OrderSource.WOOCOMMERCE)
    _schedule_confirmation(background, result)
    return result
