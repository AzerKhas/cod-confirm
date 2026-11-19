from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle
from app.crypto import encrypt_secret
from app.enums import OrderStatus, Role
from app.models import Shop, User
from app.security import hmac_sha256_base64
from app.services.shipping import (
    carrier_export_csv,
    import_status_csv,
    map_carrier_status,
    tracking_url,
)
from tests.factories import make_customer, make_order, make_shop

S = OrderStatus
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
PASSWORD = "owner-password-long"


@pytest.fixture(autouse=True)
def _reset() -> None:
    login_throttle._failures.clear()


def owner(client: TestClient, db: Session, shop: Shop) -> str:
    db.add(
        User(
            email=f"o{shop.id}@s.tn",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            shop_id=shop.id,
        )
    )
    db.commit()
    client.post("/login", data={"email": f"o{shop.id}@s.tn", "password": PASSWORD})
    return CSRF_RE.search(client.get("/dashboard/shipping").text).group(1)  # type: ignore[union-attr]


@pytest.mark.parametrize(("raw", "expected"), [
    ("Livré", S.DELIVERED), ("LIVREE", S.DELIVERED), ("delivered", S.DELIVERED), ("Payé", S.DELIVERED),
    ("Retour", S.REFUSED), ("Retourné à l'expéditeur", S.REFUSED), ("Refusé", S.REFUSED), ("Annulé", S.REFUSED),
    ("En cours de livraison", None), ("En livraison", None), ("En transit", None), ("Au dépôt", None), ("", None),
])  # fmt: skip
def test_map_carrier_status(raw: str, expected: Any) -> None:
    assert map_carrier_status(raw) is expected


def test_tracking_url(db_session: Session) -> None:
    shop = make_shop(
        db_session, tracking_url_pattern="https://suivi.example.tn/colis?code={tracking}"
    )
    order = make_order(db_session, shop, tracking_number="AB 12/3")
    assert tracking_url(shop, order) == "https://suivi.example.tn/colis?code=AB%2012%2F3"
    shop.tracking_url_pattern = "javascript:alert(1)//{tracking}"
    assert tracking_url(shop, order) is None


def test_ship_selection_and_documents(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session, name="Boutique Yasmine", default_carrier="intigo")
    a = make_order(
        db_session,
        shop,
        make_customer(db_session, shop, name="Ahmed"),
        status=S.CONFIRMED,
        city="Sfax",
    )
    b = make_order(db_session, shop, status=S.CONFIRMED)
    waiting = make_order(db_session, shop, status=S.AWAITING_CONFIRMATION)
    token = owner(client, db_session, shop)

    page = client.get("/dashboard/shipping").text
    assert (
        "À expédier" in page and f'value="{a.id}"' in page and f'value="{waiting.id}"' not in page
    )

    manifest = client.get(f"/dashboard/shipping/manifest?ids={a.id}&ids={b.id}")
    assert (
        manifest.status_code == 200
        and f"CC-{a.id}" in manifest.text
        and "Signature livreur" in manifest.text
    )
    labels = client.get(f"/dashboard/shipping/labels?ids={a.id}")
    assert "Ahmed" in labels.text and "SFAX" in labels.text.upper()
    csv_body = client.get("/dashboard/shipping/export.csv").content.decode("utf-8-sig")
    assert csv_body.startswith("Référence;Nom;Téléphone") and f"CC-{b.id}" in csv_body

    r = client.post("/dashboard/shipping/ship", data={"csrf_token": token, "carrier": "intigo",
                                                     "ids": [str(a.id), str(waiting.id)]}, follow_redirects=False)  # fmt: skip
    assert "msg=shipped" in r.headers["location"]
    db_session.expire_all()
    assert a.status is S.SHIPPED and a.carrier == "intigo" and a.shipped_at is not None
    assert waiting.status is S.AWAITING_CONFIRMATION  # only confirmed orders can be shipped
    assert b.status is S.CONFIRMED
    assert any(
        "livreur" in msg.content for msg in a.messages
    )  # WhatsApp "shipped" notice (DRY_RUN)


def test_ship_one_with_tracking(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop, status=S.CONFIRMED)
    token = owner(client, db_session, shop)
    assert 'name="tracking_number"' in client.get(f"/dashboard/orders/{order.id}").text
    client.post(f"/dashboard/orders/{order.id}/ship", data={"csrf_token": token, "carrier": "aramex",
                                                            "tracking_number": "4412345"})  # fmt: skip
    db_session.expire_all()
    assert order.status is S.SHIPPED and order.tracking_number == "4412345"
    r = client.post(f"/dashboard/orders/{order.id}/ship", data={"csrf_token": token, "carrier": "aramex"},
                    follow_redirects=False)  # fmt: skip
    assert "msg=invalid" in r.headers["location"]  # already shipped


def test_import_status_file(db_session: Session) -> None:
    shop = make_shop(db_session)
    o1 = make_order(db_session, shop, status=S.SHIPPED, tracking_number="TRK-1")
    o2 = make_order(db_session, shop, status=S.SHIPPED)
    o3 = make_order(db_session, shop, status=S.SHIPPED, tracking_number="TRK-3")
    other = make_order(db_session, make_shop(db_session), status=S.SHIPPED, tracking_number="TRK-X")
    data = (f"Code suivi;Référence;Statut\nTRK-1;;Livré\n;CC-{o2.id};Retour\nTRK-3;;En cours de livraison\n"
            f"TRK-X;;Livré\nNOPE;;Livré\n").encode("cp1252")  # fmt: skip
    report = import_status_csv(db_session, shop, data)
    assert (report.delivered, report.refused, report.unchanged) == (1, 1, 1)
    assert report.not_found == ["TRK-X", "NOPE"]  # another shop's parcel is never touched
    assert o1.status is S.DELIVERED and o2.status is S.REFUSED and o3.status is S.SHIPPED
    assert other.status is S.SHIPPED
    assert o2.customer.total_refused == 1  # risk score updated


def test_import_errors(db_session: Session) -> None:
    shop = make_shop(db_session)
    assert import_status_csv(db_session, shop, b"a,b\n1,2").error
    assert import_status_csv(db_session, shop, b"").error
    assert import_status_csv(db_session, shop, b"x" * 2_000_001).error


def test_import_via_dashboard(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop, status=S.SHIPPED, tracking_number="T9")
    token = owner(client, db_session, shop)
    r = client.post("/dashboard/shipping/import", data={"csrf_token": token},
                    files={"file": ("statuts.csv", b"tracking,status\nT9,delivered\n", "text/csv")})  # fmt: skip
    assert r.status_code == 200 and "1 livrée(s)" in r.text
    db_session.expire_all()
    assert order.status is S.DELIVERED


def test_carrier_webhook(client: TestClient, db_session: Session) -> None:
    shop = make_shop(
        db_session, slug="yasmine", carrier_webhook_secret=encrypt_secret("whsec_test")
    )
    order = make_order(db_session, shop, status=S.SHIPPED, tracking_number="NAV-77")
    db_session.commit()
    body = json.dumps({"updates": [{"tracking_number": "NAV-77", "status": "Livré"},
                                   {"reference": "CC-999999", "status": "Livré"}]}).encode()  # fmt: skip
    bad = client.post("/webhooks/carrier/yasmine", content=body, headers={"X-Signature": "nope"})
    assert bad.status_code == 401
    ok = client.post("/webhooks/carrier/yasmine", content=body,
                     headers={"X-Signature": hmac_sha256_base64("whsec_test", body)})  # fmt: skip
    assert ok.json() == {"updated": 1, "ignored": 0, "not_found": 1}
    db_session.expire_all()
    assert order.status is S.DELIVERED
    replay = client.post("/webhooks/carrier/yasmine", content=body,
                         headers={"X-Signature": hmac_sha256_base64("whsec_test", body)})  # fmt: skip
    assert replay.json()["updated"] == 0  # already delivered: idempotent


def test_webhook_secret_and_settings(client: TestClient, db_session: Session) -> None:
    shop = make_shop(db_session)
    token = owner(client, db_session, shop)
    r = client.post("/dashboard/shipping/webhook-secret", data={"csrf_token": token})
    secret = re.search(r'data-secret="webhook">([^<]+)<', r.text).group(1)  # type: ignore[union-attr]
    assert secret.startswith("whsec_")
    db_session.expire_all()
    assert shop.carrier_webhook_secret and secret not in shop.carrier_webhook_secret
    client.post("/dashboard/shipping/settings", data={"csrf_token": token, "default_carrier": "navex",
                                                      "tracking_url_pattern": "https://t.tn/{tracking}"})  # fmt: skip
    db_session.expire_all()
    assert (
        shop.default_carrier == "navex" and shop.tracking_url_pattern == "https://t.tn/{tracking}"
    )
    bad = client.post("/dashboard/shipping/settings", data={"csrf_token": token, "tracking_url_pattern": "http://x"},
                      follow_redirects=False)  # fmt: skip
    assert "msg=invalid" in bad.headers["location"]


def test_tenant_isolation_on_documents(client: TestClient, db_session: Session) -> None:
    mine = make_shop(db_session)
    other_order = make_order(db_session, make_shop(db_session), status=S.CONFIRMED)
    owner(client, db_session, mine)
    assert (
        f"CC-{other_order.id}"
        not in client.get(f"/dashboard/shipping/manifest?ids={other_order.id}").text
    )
    assert (
        f"CC-{other_order.id}"
        not in client.get(f"/dashboard/shipping/export.csv?ids={other_order.id}").text
    )


def test_export_is_formula_safe(db_session: Session) -> None:
    shop = make_shop(db_session)
    order = make_order(db_session, shop, make_customer(db_session, shop, name="=cmd|' /C calc'!A0"))
    assert "'=cmd" in carrier_export_csv([order])
