"""WhatsApp Business Platform - Cloud API client (official Graph API only).

    POST https://graph.facebook.com/{version}/{phone_number_id}/messages

* `send_template`  business-initiated message (pre-approved template, any time)
* `send_text`      free-form text (only inside the 24h customer-service window)
* `send_buttons`   interactive reply buttons (only inside the 24h window)
* `mark_as_read`   blue ticks on an incoming message

Retries with exponential backoff on network errors, HTTP 429/5xx and Meta's transient
error codes. Permanent errors (bad number, closed 24h window, wrong template...) are
raised at once. In DRY_RUN mode nothing is sent: the payload is logged (phone masked)
and a fake message id is returned.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.crypto import decrypt_secret
from app.models import Shop
from app.security import mask_phone

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"

# Meta error codes worth retrying (rate limits / temporary failures).
RETRYABLE_META_CODES = frozenset({1, 2, 4, 17, 341, 80007, 130429, 131000, 131016})
# Re-engagement error: the 24h window is closed, a template is required.
WINDOW_CLOSED_CODE = 131047


class WhatsAppError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable

    @property
    def window_closed(self) -> bool:
        return self.code == WINDOW_CLOSED_CODE


@dataclass(frozen=True)
class SendResult:
    message_id: str
    dry_run: bool = False


@dataclass(frozen=True)
class ReplyButton:
    id: str  # payload returned in the webhook, max 256 chars
    title: str  # max 20 chars

    def __post_init__(self) -> None:
        if not 1 <= len(self.title) <= 20:
            raise ValueError("button title must be 1-20 characters")
        if not 1 <= len(self.id) <= 256:
            raise ValueError("button id must be 1-256 characters")


def to_wa_number(phone_e164: str) -> str:
    """'+21698123456' -> '21698123456' (format used by the Cloud API)."""
    return phone_e164.lstrip("+")


class WhatsAppClient:
    def __init__(
        self,
        phone_number_id: str | None,
        access_token: str | None,
        *,
        settings: Settings | None = None,
        dry_run: bool | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.phone_number_id = phone_number_id
        self._token = access_token
        self.dry_run = self.settings.dry_run if dry_run is None else dry_run
        if not self.dry_run and not (phone_number_id and access_token):
            raise WhatsAppError("shop is not connected to WhatsApp (phone_number_id/token missing)")
        self._http = http or httpx.Client(timeout=self.settings.whatsapp_timeout_seconds)
        self._sleep = sleep

    # ------------------------------------------------------------ public API
    def send_template(
        self,
        to: str,
        name: str,
        language_code: str,
        body_params: Sequence[str] = (),
        button_payloads: Sequence[str] = (),
    ) -> SendResult:
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            )
        for index, payload in enumerate(button_payloads):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(index),
                    "parameters": [{"type": "payload", "payload": payload}],
                }
            )
        template: dict[str, Any] = {"name": name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        return self._send_message(to, {"type": "template", "template": template})

    def send_text(self, to: str, body: str) -> SendResult:
        if not 1 <= len(body) <= 4096:
            raise ValueError("text body must be 1-4096 characters")
        return self._send_message(
            to, {"type": "text", "text": {"preview_url": False, "body": body}}
        )

    def send_buttons(self, to: str, body: str, buttons: Sequence[ReplyButton]) -> SendResult:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("1 to 3 reply buttons allowed")
        if not 1 <= len(body) <= 1024:
            raise ValueError("button message body must be 1-1024 characters")
        interactive = {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.id, "title": b.title}} for b in buttons
                ]
            },
        }
        return self._send_message(to, {"type": "interactive", "interactive": interactive})

    def mark_as_read(self, message_id: str) -> None:
        payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if self.dry_run:
            log.info("whatsapp[dry-run]: mark as read", extra={"wa_message_id": message_id})
            return
        self._post(payload)

    def close(self) -> None:
        self._http.close()

    # -------------------------------------------------------------- internals
    def _send_message(self, to: str, message: dict[str, Any]) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_number(to),
            **message,
        }
        if self.dry_run:
            fake_id = f"dryrun.{uuid.uuid4().hex}"
            log.info(
                "whatsapp[dry-run]: message not sent",
                extra={"to": mask_phone(to), "type": message["type"], "payload": message,
                       "wa_message_id": fake_id},
            )  # fmt: skip
            return SendResult(message_id=fake_id, dry_run=True)

        data = self._post(payload)
        try:
            message_id = str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise WhatsAppError("unexpected Cloud API response (no message id)") from exc
        log.info(
            "whatsapp: message sent",
            extra={"to": mask_phone(to), "type": message["type"], "wa_message_id": message_id},
        )
        return SendResult(message_id=message_id)

    def _url(self) -> str:
        return f"{GRAPH_URL}/{self.settings.meta_graph_api_version}/{self.phone_number_id}/messages"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = self.settings.whatsapp_max_attempts
        last_error: WhatsAppError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._post_once(payload)
            except WhatsAppError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts:
                    raise
                delay = min(2 ** (attempt - 1), 8)  # 1s, 2s, 4s, 8s...
                log.warning(
                    "whatsapp: transient error, retrying",
                    extra={"attempt": attempt, "delay_s": delay, "code": exc.code,
                           "status_code": exc.status_code},
                )  # fmt: skip
                self._sleep(delay)
        raise last_error or WhatsAppError("unreachable")  # pragma: no cover

    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        try:
            response = self._http.post(self._url(), json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"network error: {type(exc).__name__}", retryable=True) from exc

        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            data = {}

        if response.is_success:
            return data

        err = data.get("error") or {}
        code = err.get("code")
        retryable = response.status_code == 429 or response.status_code >= 500
        retryable = retryable or code in RETRYABLE_META_CODES
        # Never include the token or the full phone number in the error text.
        message = f"Cloud API error {response.status_code}"
        if code is not None:
            message += f" code={code}"
        if err.get("message"):
            message += f": {str(err['message'])[:200]}"
        raise WhatsAppError(
            message, status_code=response.status_code, code=code, retryable=retryable
        )


def client_for_shop(shop: Shop, settings: Settings | None = None, **kwargs: Any) -> WhatsAppClient:
    """Build a client from the shop's encrypted credentials."""
    settings = settings or get_settings()
    token = (
        decrypt_secret(shop.access_token_encrypted, settings)
        if shop.access_token_encrypted
        else None
    )
    return WhatsAppClient(shop.whatsapp_phone_number_id, token, settings=settings, **kwargs)
