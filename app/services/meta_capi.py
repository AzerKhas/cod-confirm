"""Meta Pixel + Conversions API for the hosted order form.

Why server-side: most COD orders are never paid, so a browser "Purchase" on submit teaches
Meta's ads to find people who order and then refuse. Here the browser only sends PageView /
Lead, and the server sends one Purchase per order *after* the customer confirmed it on
WhatsApp (or after delivery, per shop setting). Nothing is collected or sent without the
customer's consent on the form.

API (developers.facebook.com/docs/marketing-api/conversions-api):
    POST https://graph.facebook.com/{version}/{pixel_id}/events?access_token=...
    {"data": [event, ...], "test_event_code": "TEST123"?}
Contact fields (ph, ct, country) are SHA-256 of the normalized value; client_ip_address,
client_user_agent, fbp and fbc are sent as is. `event_time` is in seconds and may be at most
7 days old; `event_source_url` and `client_user_agent` are required for "website" events.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import reveal_secret
from app.enums import OrderSource, OrderStatus
from app.models import Order, Shop

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"
PIXEL_ID_RE = re.compile(r"^\d{5,20}$")
# _fbp / _fbc cookie values: fb.<subdomain index>.<creation time>.<random / fbclid>
BROWSER_ID_RE = re.compile(r"^fb\.\d\.\d{10,16}\.[A-Za-z0-9_-]{1,500}$")
TEST_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
EVENT_ON = {
    "confirmed": (OrderStatus.CONFIRMED, OrderStatus.SHIPPED, OrderStatus.DELIVERED),
    "delivered": (OrderStatus.DELIVERED,),
}
# Orders older than this are never reported (stale data, and Meta refuses old events).
MAX_ORDER_AGE = timedelta(days=30)
BATCH = 50


class CapiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        """Client errors (bad token / pixel id / payload) won't fix themselves; 429 will."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_city(city: str) -> str:
    """Lowercase a-z only, no spaces or punctuation ("Ben Arous" -> "benarous")."""
    ascii_city = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", ascii_city.lower())


def is_enabled(shop: Shop) -> bool:
    return bool(shop.meta_pixel_id)


def capi_ready(shop: Shop) -> bool:
    return bool(shop.meta_pixel_id and shop.meta_capi_token)


def browser_meta(
    form: dict[str, str], *, ip: str | None, user_agent: str | None, url: str
) -> dict[str, Any]:
    """What the form keeps for ad matching (only called when the customer consented)."""
    meta: dict[str, Any] = {"url": url[:500]}
    for key in ("fbp", "fbc"):
        value = form.get(key, "").strip()
        if BROWSER_ID_RE.match(value):
            meta[key] = value
    try:
        meta["ip"] = str(ipaddress.ip_address((ip or "").strip()))
    except ValueError:
        pass  # "unknown", test clients...
    if user_agent:
        meta["ua"] = user_agent[:400]
    return meta


def purchase_event(order: Order, now: datetime) -> dict[str, Any]:
    meta = order.ad_meta or {}
    phone = re.sub(r"\D", "", order.customer.phone_e164).lstrip("0")  # 216XXXXXXXX
    user_data: dict[str, Any] = {"ph": [sha256(phone)], "country": [sha256("tn")]}
    city = normalize_city(order.city)
    if city:
        user_data["ct"] = [sha256(city)]
    for src, dst in (("ip", "client_ip_address"), ("ua", "client_user_agent"),
                     ("fbp", "fbp"), ("fbc", "fbc")):  # fmt: skip
        if meta.get(src):
            user_data[dst] = meta[src]
    contents = [
        {"id": str(item.get("name", ""))[:100], "quantity": int(item.get("qty", 1)),
         "item_price": float(item.get("price", 0) or 0)}
        for item in order.items or []
    ]  # fmt: skip
    return {
        "event_name": "Purchase",
        "event_time": int(now.timestamp()),
        "event_id": f"purchase-{order.id}",
        "action_source": "website",
        "event_source_url": meta.get("url") or "",
        "user_data": user_data,
        "custom_data": {
            "value": float(order.total),
            "currency": order.currency or "TND",
            "order_id": str(order.id),
            "content_type": "product",
            "contents": contents,
        },
    }


def send_events(shop: Shop, events: list[dict[str, Any]], *, timeout: float = 10.0) -> int:
    """POST events to the shop's pixel; returns `events_received`. Raises CapiError."""
    token = reveal_secret(shop.meta_capi_token)
    if not token or not shop.meta_pixel_id:
        raise CapiError("pixel or token missing")
    version = get_settings().meta_graph_api_version
    body: dict[str, Any] = {"data": events}
    if shop.meta_test_event_code:
        body["test_event_code"] = shop.meta_test_event_code
    try:
        r = httpx.post(
            f"{GRAPH_URL}/{version}/{shop.meta_pixel_id}/events",
            params={"access_token": token},
            json=body,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CapiError(f"network: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            detail = f"{err.get('code')}: {str(err.get('message', ''))[:200]}"
        except ValueError:
            detail = r.text[:200]
        raise CapiError(f"HTTP {r.status_code} {detail}", r.status_code)
    try:
        return int(r.json().get("events_received", 0))
    except (ValueError, AttributeError):
        return 0


def pending_purchases(db: Session, shop: Shop, now: datetime) -> list[Order]:
    statuses = EVENT_ON.get(shop.meta_event_on, EVENT_ON["confirmed"])
    return list(
        db.scalars(
            select(Order)
            .where(
                Order.shop_id == shop.id,
                Order.source == OrderSource.FORM,
                Order.ad_consent.is_(True),
                Order.capi_state.is_(None),
                Order.status.in_(statuses),
                Order.created_at >= now - MAX_ORDER_AGE,
            )
            .order_by(Order.id)
            .limit(BATCH)
        )
    )


def send_pending_purchases(db: Session, shop: Shop, now: datetime) -> int:
    """Scheduler job: one Purchase per consented order that reached the chosen status.

    A network error or a 5xx is retried on the next tick; a 4xx (bad token, bad pixel id)
    marks the batch "error" so a misconfiguration never loops forever.
    """
    if not capi_ready(shop):
        return 0
    orders = pending_purchases(db, shop, now)
    if not orders:
        return 0
    try:
        send_events(shop, [purchase_event(o, now) for o in orders])
    except CapiError as exc:
        log.warning("capi: send failed", extra={"shop_id": shop.id, "error": str(exc),
                                                "permanent": exc.permanent})  # fmt: skip
        if exc.permanent:
            for o in orders:
                o.capi_state = "error"
            db.commit()
        return 0
    for o in orders:
        o.capi_state, o.capi_sent_at = "sent", now
    db.commit()
    log.info("capi: purchases sent", extra={"shop_id": shop.id, "count": len(orders)})
    return len(orders)


def test_connection(shop: Shop) -> int:
    """Send one test PageView (only meaningful with a test event code)."""
    now = datetime.now(UTC)
    base = (get_settings().public_base_url or "").rstrip("/")
    event = {
        "event_name": "PageView",
        "event_time": int(now.timestamp()),
        "event_id": f"cc-test-{int(now.timestamp())}",
        "action_source": "website",
        "event_source_url": f"{base}/form/{shop.slug}",
        "user_data": {"client_user_agent": "COD Confirm connection test",
                      "country": [sha256("tn")]},
    }  # fmt: skip
    return send_events(shop, [event])
