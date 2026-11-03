"""Alerts to the shop owner (modification requests, questions, daily summary).

Channel: a Telegram bot (free, instant, no 24h-window rule). One bot for the whole SaaS
(TELEGRAM_BOT_TOKEN); each shop stores its owner's chat id. The dashboard always shows
the same information, so a missing / failing Telegram setup never loses anything.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings, get_settings
from app.models import Shop

log = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def notify_owner(
    shop: Shop,
    text: str,
    *,
    settings: Settings | None = None,
    http: httpx.Client | None = None,
) -> bool:
    """Send `text` to the shop owner. Returns True when delivered (or logged in DRY_RUN)."""
    settings = settings or get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    chat_id = shop.owner_telegram_chat_id

    if settings.dry_run or not token or not chat_id:
        log.info(
            "alert[not sent]: owner notification",
            extra={"shop_id": shop.id, "reason": "dry_run" if settings.dry_run else "not set",
                   "chars": len(text)},
        )  # fmt: skip
        return settings.dry_run

    client = http or httpx.Client(timeout=10)
    try:
        r = client.post(
            TELEGRAM_URL.format(token=token), json={"chat_id": chat_id, "text": text[:4000]}
        )
        ok = r.is_success
    except httpx.HTTPError:
        ok = False
    finally:
        if http is None:
            client.close()
    if not ok:
        log.warning("alert: telegram send failed", extra={"shop_id": shop.id})
    return ok
