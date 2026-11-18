from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle
from app.config import Settings, get_settings
from app.enums import Role
from app.models import Payment, Shop, User
from app.plans import price_millimes
from app.services import billing as b
from app.services.payments import (
    KONNECT_SANDBOX,
    CallbackUrls,
    FlouciProvider,
    KonnectProvider,
    Verification,
)
from tests.factories import make_shop

PASSWORD = "owner-password-long"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FLOUCI = "https://developers.flouci.com/api/v2"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    login_throttle._failures.clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(b, "notify_owner", lambda shop, text, **_: sent.append(text) or True)
    return sent


def owner_client(client: TestClient, db: Session, shop: Shop, role: Role = Role.OWNER) -> str:
    email = f"{role.value}{shop.id}@shop.tn"
    db.add(User(email=email, password_hash=hash_password(PASSWORD), role=role, shop_id=shop.id))
    db.commit()
    client.post("/login", data={"email": email, "password": PASSWORD})
    m = CSRF_RE.search(client.get("/dashboard").text)
    assert m
    return m.group(1)


def enable_konnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KONNECT_API_KEY", "konnect-key")
    monkeypatch.setenv("KONNECT_WALLET_ID", "wallet-123")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.codconfirm.tn")
    get_settings.cache_clear()


# --------------------------------------------------------------- pricing
@pytest.mark.parametrize(("plan", "months", "millimes"), [
    ("starter", 1, 49_000), ("starter", 3, 140_000), ("starter", 12, 490_000),
    ("pro", 1, 129_000), ("pro", 12, 1_290_000), ("business", 3, 995_000),
])  # fmt: skip
def test_prices(plan: str, months: int, millimes: int) -> None:
    assert price_millimes(plan, months) == millimes


@pytest.mark.parametrize(("plan", "months"), [("trial", 1), ("pro", 2), ("pro", 0)])
def test_invalid_prices(plan: str, months: int) -> None:
    with pytest.raises(ValueError):
        price_millimes(plan, months)


@pytest.mark.parametrize(("day", "months", "expected"), [
    (date(2026, 1, 31), 1, date(2026, 2, 28)), (date(2028, 1, 31), 1, date(2028, 2, 29)),
    (date(2026, 11, 18), 3, date(2027, 2, 18)), (date(2026, 12, 15), 12, date(2027, 12, 15)),
])  # fmt: skip
def test_add_months(day: date, months: int, expected: date) -> None:
    assert b.add_months(day, months) == expected


# ------------------------------------------------------------- providers
URLS = CallbackUrls("https://x/s", "https://x/f", "https://x/billing/konnect/notify")


@respx.mock
def test_konnect_create_and_verify() -> None:
    init = respx.post(f"{KONNECT_SANDBOX}/payments/init-payment").mock(
        return_value=httpx.Response(200, json={"payUrl": "https://pay/k", "paymentRef": "REF1"})
    )
    respx.get(f"{KONNECT_SANDBOX}/payments/REF1").mock(side_effect=[
        httpx.Response(200, json={"payment": {"status": "pending", "amount": 49000}}),
        httpx.Response(200, json={"payment": {"status": "completed", "amount": 49000, "reachedAmount": 49000}}),
    ])  # fmt: skip
    s = Settings(konnect_api_key="konnect-key", konnect_wallet_id="w1", _env_file=None)  # type: ignore[arg-type]
    k = KonnectProvider(s)
    pay = Payment(id=7, plan="starter", months=1, amount_millimes=49000)
    checkout = k.create(pay, Shop(name="Boutique"), URLS)
    assert checkout.ref == "REF1" and checkout.url == "https://pay/k"
    req = init.calls.last.request
    body = json.loads(req.content)
    assert req.headers["x-api-key"] == "konnect-key"
    assert body["amount"] == 49000 and body["token"] == "TND" and body["receiverWalletId"] == "w1"
    assert body["orderId"] == "7" and body["webhook"].endswith("/billing/konnect/notify")
    assert k.verify("REF1") == Verification("pending")
    assert k.verify("REF1") == Verification("paid", 49000)


@respx.mock
def test_flouci_create_and_verify() -> None:
    gen = respx.post(f"{FLOUCI}/generate_payment").mock(return_value=httpx.Response(200, json={
        "result": {"success": True, "payment_id": "FP1", "link": "https://checkout.flouci.com/x/FP1"}}))  # fmt: skip
    respx.get(f"{FLOUCI}/verify_payment/FP1").mock(side_effect=[
        httpx.Response(200, json={"success": True, "result": {"status": "PENDING", "amount": 129000}}),
        httpx.Response(200, json={"success": True, "result": {"status": "SUCCESS", "amount": 129000}}),
        httpx.Response(200, json={"success": True, "result": {"status": "EXPIRED"}}),
    ])  # fmt: skip
    s = Settings(flouci_public_key="pub", flouci_private_key="priv", _env_file=None)  # type: ignore[arg-type]
    f = FlouciProvider(s)
    checkout = f.create(
        Payment(id=9, plan="pro", months=1, amount_millimes=129000), Shop(name="B"), URLS
    )
    assert checkout.ref == "FP1"
    req = gen.calls.last.request
    assert req.headers["authorization"] == "Bearer pub:priv"
    assert json.loads(req.content)["amount"] == "129000"
    assert [f.verify("FP1").status for _ in range(3)] == ["pending", "paid", "expired"]


@respx.mock
def test_provider_errors() -> None:
    respx.post(f"{FLOUCI}/generate_payment").mock(
        return_value=httpx.Response(400, json={"result": {"status": 400}})
    )
    respx.get(f"{FLOUCI}/verify_payment/X").mock(
        return_value=httpx.Response(404, json={"result": {"status": 404}})
    )
    f = FlouciProvider(Settings(flouci_public_key="p", flouci_private_key="k", _env_file=None))  # type: ignore[arg-type]
    with pytest.raises(b.PaymentProviderError):
        f.create(Payment(id=1, plan="pro", months=1, amount_millimes=1), Shop(name="B"), URLS)
    with pytest.raises(b.PaymentProviderError):
        f.verify("X")


# ------------------------------------------------------------- demo flow
def test_demo_checkout_end_to_end(
    client: TestClient, db_session: Session, no_alerts: list[str]
) -> None:
    shop = make_shop(db_session, plan="trial", paid_until=date.today() + timedelta(days=5))
    token = owner_client(client, db_session, shop)
    page = client.get("/dashboard/billing")
    assert page.status_code == 200 and "Choisir un plan" in page.text and "129" in page.text

    r = client.post("/dashboard/billing/checkout", data={"csrf_token": token, "plan": "pro", "months": "3",
                                                        "provider": "demo"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].startswith("/billing/demo/")
    payment = db_session.scalars(select(Payment)).one()
    assert payment.amount_millimes == price_millimes("pro", 3) and payment.status == "pending"

    assert "Simuler un paiement" in client.get(r.headers["location"]).text
    done = client.post(
        r.headers["location"],
        data={"csrf_token": token, "result": "success"},
        follow_redirects=False,
    )
    assert done.headers["location"] == "/dashboard/billing?msg=paid"
    db_session.expire_all()
    today = b.today_for(shop)
    assert shop.plan == "pro" and shop.is_active
    assert shop.paid_until == b.add_months(today, 3)  # trial -> starts today
    assert payment.invoice_number and payment.invoice_number.startswith(f"CC-{today.year}-")
    assert no_alerts and "Paiement reçu" in no_alerts[0]

    # Replaying the success never extends twice.
    client.post(r.headers["location"], data={"csrf_token": token, "result": "success"})
    db_session.expire_all()
    assert shop.paid_until == b.add_months(today, 3)

    receipt = client.get(f"/dashboard/billing/receipts/{payment.id}")
    assert receipt.status_code == 200 and payment.invoice_number in receipt.text


def test_checkout_validation_and_permissions(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    token = owner_client(client, db_session, shop)
    for data in ({"plan": "trial", "months": "1", "provider": "demo"},
                 {"plan": "pro", "months": "2", "provider": "demo"},
                 {"plan": "pro", "months": "1", "provider": "paypal"}):  # fmt: skip
        r = client.post(
            "/dashboard/billing/checkout",
            data={"csrf_token": token, **data},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/dashboard/billing?msg=invalid"
    assert (
        client.post(
            "/dashboard/billing/checkout", data={"plan": "pro", "months": "1", "provider": "demo"}
        ).status_code
        == 403
    )  # no CSRF
    assert db_session.scalars(select(Payment)).all() == []


def test_staff_cannot_pay(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    owner_client(client, db_session, shop, Role.STAFF)
    assert client.get("/dashboard/billing").status_code == 403


def test_demo_disabled_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    from app.services.payments import available_providers

    s = Settings(app_env="prod", secret_key="x" * 40, encryption_key=Fernet.generate_key().decode(),
                 allowed_hosts="a.tn", _env_file=None)  # type: ignore[arg-type]  # fmt: skip
    assert "demo" not in available_providers(s)


def test_receipts_of_other_shops_are_hidden(client: TestClient, db_session: Session) -> None:
    other = make_shop(db_session)
    p = Payment(shop_id=other.id, provider="manual", plan="pro", months=1, amount_millimes=1, status="paid",
                checks=0, period_start=date.today(), period_end=date.today(), paid_at=datetime.now(UTC),
                invoice_number="CC-X")  # fmt: skip
    db_session.add(p)
    db_session.commit()
    owner_client(client, db_session, make_shop(db_session))
    assert client.get(f"/dashboard/billing/receipts/{p.id}").status_code == 404


# ----------------------------------------------------------- Konnect flow
@respx.mock
def test_konnect_flow_and_webhook_replay(client: TestClient, db_session: Session,
                                         monkeypatch: pytest.MonkeyPatch, no_alerts: list[str]) -> None:  # fmt: skip
    enable_konnect(monkeypatch)
    init = respx.post(f"{KONNECT_SANDBOX}/payments/init-payment").mock(
        return_value=httpx.Response(200, json={"payUrl": "https://gateway.sandbox.konnect.network/pay?ref=K9",
                                               "paymentRef": "K9"}))  # fmt: skip
    respx.get(f"{KONNECT_SANDBOX}/payments/K9").mock(
        return_value=httpx.Response(
            200, json={"payment": {"status": "completed", "amount": 49000, "reachedAmount": 49000}}
        )
    )
    end = date.today() + timedelta(days=10)
    shop = make_shop(db_session, plan="starter", paid_until=end)
    token = owner_client(client, db_session, shop)

    r = client.post("/dashboard/billing/checkout", data={"csrf_token": token, "plan": "starter", "months": "1",
                                                        "provider": "konnect"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"].startswith("https://gateway.sandbox.konnect.network/")
    assert (
        json.loads(init.calls.last.request.content)["webhook"]
        == "https://app.codconfirm.tn/billing/konnect/notify"
    )

    back = client.get("/billing/konnect/notify?payment_ref=K9", follow_redirects=False)
    assert back.headers["location"] == "/dashboard/billing?msg=paid"
    db_session.expire_all()
    assert shop.paid_until == b.add_months(end, 1)  # renewal extends from the current end
    client.get("/billing/konnect/notify?payment_ref=K9")  # duplicate webhook
    db_session.expire_all()
    assert shop.paid_until == b.add_months(end, 1)


@respx.mock
def test_tampered_amount_is_not_applied(db_session: Session, no_alerts: list[str]) -> None:
    shop = make_shop(db_session, plan="starter", paid_until=date.today())
    p = Payment(shop_id=shop.id, provider="konnect", plan="business", months=12, status="pending",
                amount_millimes=price_millimes("business", 12), provider_ref="CHEAP", checks=0)  # fmt: skip
    db_session.add(p)
    db_session.commit()
    respx.get(f"{KONNECT_SANDBOX}/payments/CHEAP").mock(
        return_value=httpx.Response(
            200, json={"payment": {"status": "completed", "reachedAmount": 1000}}
        )
    )
    s = Settings(konnect_api_key="k", konnect_wallet_id="w", _env_file=None)  # type: ignore[arg-type]
    assert b.verify_payment(db_session, p, {"konnect": KonnectProvider(s)}) == "failed"
    assert shop.plan == "starter" and "mismatch" in (p.note or "")


def test_unknown_ref_and_forged_success(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = make_shop(db_session, plan="starter", paid_until=date.today())
    r = client.get("/billing/konnect/notify?payment_ref=NOPE", follow_redirects=False)
    assert r.headers["location"] == "/dashboard/billing?msg=pending"
    p = Payment(shop_id=shop.id, provider="flouci", plan="pro", months=1, amount_millimes=129000,
                status="pending", provider_ref="FX", checks=0)  # fmt: skip
    db_session.add(p)
    db_session.commit()

    class StillPending:
        name = "flouci"

        def verify(self, ref: str) -> Verification:
            return Verification("pending")

    monkeypatch.setattr(
        "app.services.billing.available_providers", lambda: {"flouci": StillPending()}
    )
    r = client.get(f"/billing/flouci/return?p={p.id}&result=success", follow_redirects=False)
    assert (
        r.headers["location"] == "/dashboard/billing?msg=pending"
    )  # the query string is not trusted
    db_session.expire_all()
    assert p.status == "pending" and shop.plan == "starter"
    assert client.get("/billing/flouci/notify?payment_id=FX&success=true").json() == {
        "status": "pending"
    }
    assert client.get("/billing/flouci/notify?payment_id=ZZ").json() == {"status": "ignored"}


# ---------------------------------------------------------- scheduled jobs
def test_suspension_after_grace_and_reactivation(db_session: Session, no_alerts: list[str]) -> None:
    now = datetime(2026, 11, 18, 10, tzinfo=UTC)
    shop = make_shop(db_session, plan="pro", paid_until=date(2026, 11, 14))  # 4 days ago, grace 3
    assert b.billing_jobs(db_session, shop, now)["suspended"] == 1
    assert shop.is_active is False and "expiré" in no_alerts[-1]
    assert b.billing_state(shop, date(2026, 11, 18)).status == "suspended"

    p = Payment(shop_id=shop.id, provider="manual", plan="pro", months=1, amount_millimes=129000,
                status="pending", checks=0)  # fmt: skip
    db_session.add(p)
    db_session.flush()
    b.apply_paid(db_session, p, now)
    assert shop.is_active is True and shop.paid_until == date(
        2026, 12, 18
    )  # from today, not the old end


def test_reminders_once_a_day(db_session: Session, no_alerts: list[str]) -> None:
    now = datetime(2026, 11, 18, 10, tzinfo=UTC)
    shop = make_shop(db_session, plan="starter", paid_until=date(2026, 11, 21))  # 3 days left
    assert b.billing_jobs(db_session, shop, now)["reminders"] == 1
    assert b.billing_jobs(db_session, shop, now + timedelta(hours=2))["reminders"] == 0
    assert "dans 3 jours" in no_alerts[0]
    assert b.billing_state(shop, date(2026, 11, 18)).status == "expiring"
    assert (
        b.billing_jobs(db_session, shop, now + timedelta(days=1))["reminders"] == 0
    )  # 2 days left: quiet


def test_pending_payments_checked_sparingly_then_expired(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Counting:
        name = "flouci"

        def verify(self, ref: str) -> Verification:
            calls.append(ref)
            return Verification("pending")

    monkeypatch.setattr("app.services.billing.available_providers", lambda: {"flouci": Counting()})
    shop = make_shop(db_session)
    t0 = datetime.now(UTC)
    p = Payment(shop_id=shop.id, provider="flouci", plan="pro", months=1, amount_millimes=1,
                status="pending", provider_ref="P1", checks=0, created_at=t0)  # fmt: skip
    db_session.add(p)
    db_session.commit()
    b.billing_jobs(db_session, shop, t0 + timedelta(minutes=11))
    b.billing_jobs(db_session, shop, t0 + timedelta(minutes=12))  # too soon: no new call
    assert calls == ["P1"]
    for i in range(10):
        b.billing_jobs(db_session, shop, t0 + timedelta(minutes=30 + 11 * i))
    assert len(calls) == b.MAX_CHECKS
    b.billing_jobs(db_session, shop, t0 + timedelta(hours=25))
    assert p.status == "expired"


# ------------------------------------------------------------------ admin UI
def test_admin_manual_payment(
    client: TestClient, db_session: Session, no_alerts: list[str]
) -> None:
    shop = make_shop(db_session, plan="trial", is_active=False)
    db_session.add(
        User(email="ops@x.tn", password_hash=hash_password(PASSWORD), role=Role.SUPERADMIN)
    )
    db_session.commit()
    client.post("/login", data={"email": "ops@x.tn", "password": PASSWORD})
    token = CSRF_RE.search(client.get(f"/admin/shops/{shop.id}").text).group(1)  # type: ignore[union-attr]
    r = client.post(f"/admin/shops/{shop.id}/payments", data={"csrf_token": token, "plan": "pro", "months": "12",
                                                              "amount": "1290", "note": "virement"},
                    follow_redirects=False)  # fmt: skip
    assert "msg=saved" in r.headers["location"]
    db_session.expire_all()
    assert shop.plan == "pro" and shop.is_active
    pay = db_session.scalars(select(Payment)).one()
    assert pay.provider == "manual" and pay.amount_millimes == 1_290_000 and pay.status == "paid"


def test_new_trial_shop_gets_trial_end(client: TestClient, db_session: Session) -> None:
    db_session.add(
        User(email="ops@x.tn", password_hash=hash_password(PASSWORD), role=Role.SUPERADMIN)
    )
    db_session.commit()
    client.post("/login", data={"email": "ops@x.tn", "password": PASSWORD})
    token = CSRF_RE.search(client.get("/admin/shops/new").text).group(1)  # type: ignore[union-attr]
    client.post("/admin/shops/new", data={"csrf_token": token, "name": "Essai", "slug": "essai-shop",
                                          "plan": "trial", "default_language": "fr", "owner_email": "o@e.tn"})  # fmt: skip
    shop = db_session.scalar(select(Shop).where(Shop.slug == "essai-shop"))
    assert shop is not None and shop.paid_until == b.today_for(shop) + timedelta(days=14)


def test_dashboard_banner_when_expiring(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, plan="starter", paid_until=date.today() + timedelta(days=2))
    owner_client(client, db_session, shop)
    page = client.get("/dashboard").text
    assert "se termine dans" in page and "Renouveler" in page


def test_prod_requires_https_public_url_for_real_payments() -> None:
    from cryptography.fernet import Fernet

    base = {"app_env": "prod", "secret_key": "x" * 40, "encryption_key": Fernet.generate_key().decode(),
            "allowed_hosts": "a.tn", "konnect_api_key": "k", "_env_file": None}  # fmt: skip
    with pytest.raises(ValueError):
        Settings(**base)  # type: ignore[arg-type]
    assert Settings(**base, public_base_url="https://app.codconfirm.tn").konnect_sandbox is True  # type: ignore[arg-type]
