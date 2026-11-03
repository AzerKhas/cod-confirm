from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password, login_throttle, verify_password
from app.enums import Direction, MessageType, OrderStatus, Role
from app.models import Customer, MessageLog, Order, User
from tests.factories import make_customer, make_order, make_shop

S = OrderStatus
PASSWORD = "very-secret-password"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_user(
    db: Session, shop: Any, email: str = "owner@shop.tn", role: Role = Role.OWNER
) -> User:
    user = User(email=email, password_hash=hash_password(PASSWORD), role=role,
                shop_id=shop.id if shop else None)  # fmt: skip
    db.add(user)
    db.commit()
    return user


def login(client: TestClient, email: str = "owner@shop.tn", password: str = PASSWORD) -> Any:
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def csrf(client: TestClient, url: str = "/dashboard") -> str:
    match = CSRF_RE.search(client.get(url).text)
    assert match, "csrf token not found"
    return match.group(1)


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    login_throttle._failures.clear()


@pytest.fixture
def shop_world(db_session: Session) -> dict[str, Any]:
    shop = make_shop(db_session, name="Boutique Yasmine", delivery_fee=Decimal("7"))
    other = make_shop(db_session, name="Autre boutique")
    customer = make_customer(db_session, shop, name="Ahmed Ben Salah", phone_e164="+21698123456")
    order = make_order(db_session, shop, customer, status=S.AWAITING_CONFIRMATION)
    other_order = make_order(db_session, other)
    db_session.add(MessageLog(order=order, customer_id=customer.id, direction=Direction.OUT,
                              type=MessageType.TEMPLATE, content="Salam Ahmed 👋 commande"))  # fmt: skip
    make_user(db_session, shop)
    make_user(db_session, other, email="other@shop.tn")
    db_session.commit()
    return {"shop": shop, "other": other, "order": order, "other_order": other_order,
            "customer": customer}  # fmt: skip


# ----------------------------------------------------------------------- auth
def test_password_hashing() -> None:
    h = hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert verify_password(PASSWORD, h)
    assert not verify_password("wrong-password", h)
    assert not verify_password(PASSWORD, "garbage")
    with pytest.raises(ValueError):
        hash_password("short")


def test_dashboard_requires_login(client: TestClient) -> None:
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_logout(client: TestClient, shop_world: dict[str, Any]) -> None:
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Boutique Yasmine" in page.text
    client.post("/logout", data={"csrf_token": csrf(client)})
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_wrong_password_and_throttle(client: TestClient, shop_world: dict[str, Any]) -> None:
    for _ in range(5):
        assert login(client, password="nope-nope-nope").status_code == 401
    assert login(client).status_code == 429  # even the right password is blocked for a while


def test_inactive_user_cannot_login(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    user = db_session.scalar(select(User).where(User.email == "owner@shop.tn"))
    assert user is not None
    user.is_active = False
    db_session.commit()
    assert login(client).status_code == 401


def test_session_revoked_by_version_bump(client: TestClient, db_session: Session,
                                         shop_world: dict[str, Any]) -> None:  # fmt: skip
    login(client)
    user = db_session.scalar(select(User).where(User.email == "owner@shop.tn"))
    assert user is not None
    user.session_version += 1
    db_session.commit()
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_csrf_required(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    order = shop_world["order"]
    r = client.post(f"/dashboard/orders/{order.id}/status", data={"to": "CANCELLED"})
    assert r.status_code == 403
    r = client.post(
        f"/dashboard/orders/{order.id}/status", data={"to": "CANCELLED", "csrf_token": "x"}
    )
    assert r.status_code == 403
    assert order.status is S.AWAITING_CONFIRMATION


# -------------------------------------------------------------- tenant isolation
def test_cannot_see_other_shop_order(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    other_id = shop_world["other_order"].id
    assert client.get(f"/dashboard/orders/{other_id}").status_code == 404
    token = csrf(client)
    r = client.post(
        f"/dashboard/orders/{other_id}/status", data={"to": "CANCELLED", "csrf_token": token}
    )
    assert r.status_code == 404
    listing = client.get("/dashboard/orders").text
    assert f">{other_id}<" not in listing


def test_cannot_delete_other_shop_customer(client: TestClient, db_session: Session,
                                           shop_world: dict[str, Any]) -> None:  # fmt: skip
    login(client)
    other_customer_id = shop_world["other_order"].customer_id
    r = client.post(
        f"/dashboard/customers/{other_customer_id}/delete", data={"csrf_token": csrf(client)}
    )
    assert r.status_code == 404
    assert db_session.get(Customer, other_customer_id) is not None


# --------------------------------------------------------------------- pages
def test_home_kpis(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    r = client.get("/dashboard?period=30d")
    assert "Confirmation" in r.text and "Économisé" in r.text
    assert "Ahmed Ben Salah" in r.text


def test_orders_filters_and_htmx(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    full = client.get("/dashboard/orders?status=AWAITING_CONFIRMATION")
    assert "Ahmed Ben Salah" in full.text and "<header" in full.text
    partial = client.get("/dashboard/orders?q=98123", headers={"HX-Request": "true"})
    assert "Ahmed Ben Salah" in partial.text and "<header" not in partial.text
    empty = client.get("/dashboard/orders?status=DELIVERED")
    assert "Aucune commande" in empty.text


def test_order_detail_shows_conversation(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    r = client.get(f"/dashboard/orders/{shop_world['order'].id}")
    assert "Salam Ahmed 👋 commande" in r.text
    assert "Conversation WhatsApp" in r.text


# ------------------------------------------------------------------- actions
def test_ship_deliver_flow(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    login(client)
    order: Order = shop_world["order"]
    token = csrf(client)
    for to in ("CONFIRMED", "SHIPPED", "DELIVERED"):
        r = client.post(f"/dashboard/orders/{order.id}/status", data={"to": to, "csrf_token": token},
                        follow_redirects=False)  # fmt: skip
        assert r.status_code == 303
    db_session.expire_all()
    assert order.status is S.DELIVERED
    shipped_msgs = [x for x in order.messages if "livreur" in x.content]
    assert len(shipped_msgs) == 1  # "order shipped" WhatsApp notice (DRY_RUN)
    assert order.shipped_notified_at is not None


def test_refused_updates_risk(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    login(client)
    order: Order = shop_world["order"]
    order.status = S.SHIPPED
    db_session.commit()
    client.post(
        f"/dashboard/orders/{order.id}/status", data={"to": "REFUSED", "csrf_token": csrf(client)}
    )
    db_session.expire_all()
    assert order.status is S.REFUSED
    assert order.customer.total_refused == 1
    page = client.get(f"/dashboard/orders/{order.id}").text
    assert "déjà refusé des colis" in page


def test_invalid_action_rejected(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    order = shop_world["order"]
    r = client.post(f"/dashboard/orders/{order.id}/status", data={"to": "DELIVERED", "csrf_token": csrf(client)},
                    follow_redirects=False)  # fmt: skip
    assert "msg=invalid" in r.headers["location"]
    assert order.status is S.AWAITING_CONFIRMATION


def test_edit_modify_requested_resends_confirmation(client: TestClient, db_session: Session,
                                                    shop_world: dict[str, Any]) -> None:  # fmt: skip
    login(client)
    order: Order = shop_world["order"]
    order.status = S.MODIFY_REQUESTED
    db_session.commit()
    data = {"csrf_token": csrf(client), "address": "Nouvelle adresse 5", "city": "Sfax", "total": "",
            "item_name": ["Robe bleue", ""], "item_qty": ["2", "1"], "item_price": ["45", "0"]}  # fmt: skip
    r = client.post(f"/dashboard/orders/{order.id}/edit", data=data, follow_redirects=False)
    assert "msg=edited" in r.headers["location"]
    db_session.expire_all()
    assert order.status is S.AWAITING_CONFIRMATION
    assert order.address == "Nouvelle adresse 5"
    assert order.total == Decimal("90.000")
    assert order.items == [{"name": "Robe bleue", "qty": 2, "price": "45.000"}]
    assert "Robe bleue" in order.messages[-1].content  # new confirmation with the new details


def test_edit_invalid_data(client: TestClient, shop_world: dict[str, Any]) -> None:
    login(client)
    order = shop_world["order"]
    data = {"csrf_token": csrf(client), "address": "", "city": "Sfax", "item_name": "x",
            "item_qty": "1", "item_price": "abc"}  # fmt: skip
    r = client.post(f"/dashboard/orders/{order.id}/edit", data=data, follow_redirects=False)
    assert "msg=invalid" in r.headers["location"]


def test_resend_confirmation(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    login(client)
    order: Order = shop_world["order"]
    order.status = S.NO_ANSWER
    order.reminders_sent = 2
    db_session.commit()
    r = client.post(f"/dashboard/orders/{order.id}/resend", data={"csrf_token": csrf(client)},
                    follow_redirects=False)  # fmt: skip
    assert "msg=resent" in r.headers["location"]
    db_session.expire_all()
    assert order.status is S.AWAITING_CONFIRMATION and order.reminders_sent == 0


def test_delete_customer(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    login(client)
    cid = shop_world["customer"].id
    r = client.post(f"/dashboard/customers/{cid}/delete", data={"csrf_token": csrf(client)},
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Customer, cid) is None


# ----------------------------------------------------------------------- CSV
def test_csv_export_is_safe(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    make_order(db_session, shop_world["shop"],
               make_customer(db_session, shop_world["shop"], name="=HYPERLINK(\"http://evil\")"))  # fmt: skip
    db_session.commit()
    login(client)
    r = client.get("/dashboard/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    assert "Ahmed Ben Salah" in body
    assert "'=HYPERLINK" in body  # neutralized formula
    assert "Autre boutique" not in body
    assert "+21698123456" in body


# ------------------------------------------------------------------ settings
def test_settings_owner_only(
    client: TestClient, db_session: Session, shop_world: dict[str, Any]
) -> None:
    make_user(db_session, shop_world["shop"], email="staff@shop.tn", role=Role.STAFF)
    login(client, email="staff@shop.tn")
    assert client.get("/dashboard/orders").status_code == 200
    assert client.get("/dashboard/settings").status_code == 403


def test_save_settings(client: TestClient, db_session: Session, shop_world: dict[str, Any]) -> None:
    login(client)
    data = {"csrf_token": csrf(client, "/dashboard/settings"), "name": "Yasmine Store",
            "default_language": "fr", "delivery_fee": "8,5", "reminder_1": "2", "reminder_2": "20",
            "data_retention_days": "180", "owner_telegram_chat_id": "424242",
            "products": "Robe = 45\nFoulard = 15.5"}  # fmt: skip
    r = client.post("/dashboard/settings", data=data, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]
    shop = shop_world["shop"]
    db_session.expire_all()
    assert shop.name == "Yasmine Store"
    assert shop.delivery_fee == Decimal("8.500")
    assert shop.reminder_delays == [2, 20]
    assert shop.form_products == [
        {"name": "Robe", "price": "45.000"},
        {"name": "Foulard", "price": "15.500"},
    ]

    bad = {
        **data,
        "products": "nimporte quoi = abc",
        "csrf_token": csrf(client, "/dashboard/settings"),
    }
    assert (
        "msg=invalid"
        in client.post("/dashboard/settings", data=bad, follow_redirects=False).headers["location"]
    )
