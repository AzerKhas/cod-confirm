"""Payment providers for subscriptions: Konnect and Flouci (Tunisia), plus a demo checkout.

Both real providers follow the same safe pattern:
  1. `create()` asks the provider for a hosted payment page (we redirect the seller there);
  2. the provider calls our webhook / redirects the browser back with a reference;
  3. `verify()` asks the provider's API for the real status. Query strings, redirects and
     webhooks are NEVER trusted on their own (neither provider signs its v1 callbacks).

Konnect  - docs.konnect.network : POST /payments/init-payment, GET /payments/{ref},
           header `x-api-key`, amounts in millimes, webhook = GET ?payment_ref=...
Flouci   - docs.flouci.com      : POST /api/v2/generate_payment, GET /api/v2/verify_payment/{id},
           header `Authorization: Bearer <public>:<private>`, amounts in millimes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.config import Settings, get_settings
from app.models import Payment, Shop

log = logging.getLogger(__name__)

Status = Literal["paid", "pending", "failed", "expired"]

KONNECT_PROD = "https://api.konnect.network/api/v2"
KONNECT_SANDBOX = "https://api.sandbox.konnect.network/api/v2"
FLOUCI_BASE = "https://developers.flouci.com/api/v2"
LINK_LIFESPAN_MINUTES = 30


class PaymentProviderError(RuntimeError):
    """The provider refused the request or answered something unexpected."""


@dataclass(frozen=True)
class Checkout:
    ref: str
    url: str


@dataclass(frozen=True)
class Verification:
    status: Status
    amount_millimes: int | None = None


@dataclass(frozen=True)
class CallbackUrls:
    success: str
    fail: str
    webhook: str


class PaymentProvider(Protocol):
    name: str
    label: str

    def create(self, payment: Payment, shop: Shop, urls: CallbackUrls) -> Checkout: ...

    def verify(self, ref: str) -> Verification: ...


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------- Konnect
class KonnectProvider:
    name = "konnect"
    label = "Konnect (carte bancaire, e-DINAR, wallet)"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.base = KONNECT_SANDBOX if settings.konnect_sandbox else KONNECT_PROD
        self._key = settings.konnect_api_key.get_secret_value()
        self.wallet_id = settings.konnect_wallet_id
        self._http = http or httpx.Client(timeout=15)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._key, "Content-Type": "application/json"}

    def create(self, payment: Payment, shop: Shop, urls: CallbackUrls) -> Checkout:
        body = {
            "receiverWalletId": self.wallet_id,
            "token": "TND",
            "amount": payment.amount_millimes,
            "type": "immediate",
            "description": f"COD Confirm - {payment.plan} {payment.months} mois - {shop.name}"[
                :250
            ],
            "acceptedPaymentMethods": ["wallet", "bank_card", "e-DINAR"],
            "lifespan": LINK_LIFESPAN_MINUTES,
            "checkoutForm": False,
            "addPaymentFeesToAmount": False,
            "orderId": str(payment.id),
            # Konnect redirects the payer to this URL (GET ?payment_ref=...) and also
            # calls it server-side; it is our return page and our webhook at once.
            "webhook": urls.webhook,
            "theme": "light",
        }
        try:
            r = self._http.post(
                f"{self.base}/payments/init-payment", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"Konnect unreachable: {type(exc).__name__}") from exc
        data = _json(r)
        if not r.is_success or not data.get("payUrl") or not data.get("paymentRef"):
            raise PaymentProviderError(f"Konnect init-payment failed (HTTP {r.status_code})")
        return Checkout(ref=str(data["paymentRef"]), url=str(data["payUrl"]))

    def verify(self, ref: str) -> Verification:
        try:
            r = self._http.get(f"{self.base}/payments/{ref}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"Konnect unreachable: {type(exc).__name__}") from exc
        payment = _json(r).get("payment")
        if not r.is_success or not isinstance(payment, dict):
            raise PaymentProviderError(f"Konnect payment lookup failed (HTTP {r.status_code})")
        # Rely on the payment status, not the transaction status (Konnect docs).
        if payment.get("status") == "completed":
            reached = payment.get("reachedAmount", payment.get("amount"))
            return Verification("paid", int(reached) if reached is not None else None)
        return Verification("pending")


# -------------------------------------------------------------------- Flouci
class FlouciProvider:
    name = "flouci"
    label = "Flouci (wallet Flouci, carte bancaire)"

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self._auth = (
            f"Bearer {settings.flouci_public_key}:{settings.flouci_private_key.get_secret_value()}"
        )
        self._http = http or httpx.Client(timeout=15)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth, "Content-Type": "application/json"}

    def create(self, payment: Payment, shop: Shop, urls: CallbackUrls) -> Checkout:
        body = {
            "amount": str(payment.amount_millimes),
            "success_link": urls.success,
            "fail_link": urls.fail,
            "webhook": urls.webhook,
            "developer_tracking_id": str(payment.id),
            "session_timeout_secs": LINK_LIFESPAN_MINUTES * 60,
            "accept_card": True,
        }
        try:
            r = self._http.post(
                f"{FLOUCI_BASE}/generate_payment", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"Flouci unreachable: {type(exc).__name__}") from exc
        result = _json(r).get("result")
        if not r.is_success or not isinstance(result, dict) or not result.get("success"):
            raise PaymentProviderError(f"Flouci generate_payment failed (HTTP {r.status_code})")
        return Checkout(ref=str(result["payment_id"]), url=str(result["link"]))

    def verify(self, ref: str) -> Verification:
        try:
            r = self._http.get(f"{FLOUCI_BASE}/verify_payment/{ref}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"Flouci unreachable: {type(exc).__name__}") from exc
        data = _json(r)
        result = data.get("result")
        # Check `success` before reading the content (Flouci docs).
        if not r.is_success or not data.get("success") or not isinstance(result, dict):
            raise PaymentProviderError(f"Flouci verify_payment failed (HTTP {r.status_code})")
        status = str(result.get("status", "")).upper()
        amount = result.get("amount")
        if status == "SUCCESS":
            return Verification("paid", int(amount) if amount is not None else None)
        if status == "EXPIRED":
            return Verification("expired")
        if status in ("FAILURE", "SYSTEM_FAILURE"):
            return Verification("failed")
        return Verification("pending")


# ---------------------------------------------------------------------- demo
class DemoProvider:
    """Fake checkout page inside the app (no money). Disabled in production."""

    name = "demo"
    label = "Démo (aucun paiement réel)"

    def create(self, payment: Payment, shop: Shop, urls: CallbackUrls) -> Checkout:
        return Checkout(ref=f"demo-{payment.id}", url=f"/billing/demo/{payment.id}")

    def verify(self, ref: str) -> Verification:
        return Verification("pending")  # only the demo page can mark it paid


def available_providers(settings: Settings | None = None) -> dict[str, PaymentProvider]:
    s = settings or get_settings()
    providers: dict[str, PaymentProvider] = {}
    if s.konnect_api_key.get_secret_value() and s.konnect_wallet_id:
        providers["konnect"] = KonnectProvider(s)
    if s.flouci_public_key and s.flouci_private_key.get_secret_value():
        providers["flouci"] = FlouciProvider(s)
    if s.payments_demo and s.app_env != "prod":
        providers["demo"] = DemoProvider()
    return providers
