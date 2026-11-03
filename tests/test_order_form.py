from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import Language, OrderSource
from app.models import Order
from app.security import make_form_token
from tests.factories import make_shop

PRODUCTS = [{"name": "Skmei 1251", "price": "59.000"}, {"name": "Skmei 1335", "price": "69.000"}]


@pytest.fixture
def shop(db_session: Session) -> Any:
    s = make_shop(db_session, name="Skmei Tunisie", slug="skmei-tn", form_products=PRODUCTS)
    db_session.commit()
    return s


def form(slug: str = "skmei-tn", **over: str) -> dict[str, str]:
    data = {"name": "Mariem Gharbi", "phone": "55 444 333", "city": "Nabeul",
            "address": "Hammamet, rue des jasmins", "product": "1", "qty": "2"}  # fmt: skip
    data["ft"] = make_form_token(slug)
    data.update(over)
    return data


def count_orders(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Order)) or 0


def test_form_renders_in_darija(client: TestClient, shop: Any) -> None:
    r = client.get("/form/skmei-tn")
    assert r.status_code == 200
    assert "Skmei Tunisie" in r.text
    assert "Commandi taw" in r.text
    assert "Skmei 1335 – 69.000 DT" in r.text
    assert "Sidi Bouzid" in r.text  # governorates list


def test_form_arabic_is_rtl(client: TestClient, shop: Any) -> None:
    r = client.get("/form/skmei-tn?lang=ar")
    assert 'dir="rtl"' in r.text
    assert "اطلب الآن" in r.text


def test_form_unknown_lang_falls_back(client: TestClient, shop: Any) -> None:
    assert "Commandi taw" in client.get("/form/skmei-tn?lang=xx").text


def test_form_unknown_shop_404(client: TestClient) -> None:
    assert client.get("/form/nope").status_code == 404


def test_submit_creates_order_and_redirects(client: TestClient, db_session: Session,
                                            shop: Any) -> None:  # fmt: skip
    r = client.post("/form/skmei-tn", data=form(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/form/skmei-tn/done?lang=darija"

    order = db_session.scalars(select(Order)).one()
    assert order.source is OrderSource.FORM
    assert order.customer.phone_e164 == "+21655444333"
    assert order.customer.language is Language.DARIJA
    assert order.items == [{"name": "Skmei 1335", "qty": 2, "price": "69.000"}]
    assert order.total == Decimal("138.000")

    done = client.get(r.headers["location"])
    assert "Ya3tik essa7a" in done.text


def test_price_cannot_be_forged(client: TestClient, db_session: Session, shop: Any) -> None:
    client.post("/form/skmei-tn", data=form(price="1", total="1"), follow_redirects=False)
    assert db_session.scalars(select(Order)).one().total == Decimal("138.000")


@pytest.mark.parametrize(
    ("over", "field_msg"),
    [
        ({"phone": "71 000 000"}, "Noumrou mouch s7i7"),
        ({"phone": ""}, "Lazem t3abbi hedhi"),
        ({"name": "  "}, "Lazem t3abbi hedhi"),
        ({"city": "Paris"}, "Lazem t3abbi hedhi"),
        ({"product": "9"}, "A5tar produit"),
        ({"qty": "0"}, "Fama ghalta"),
        ({"qty": "abc"}, "Fama ghalta"),
    ],
)
def test_invalid_submission_shows_error(client: TestClient, db_session: Session, shop: Any,
                                        over: dict[str, str], field_msg: str) -> None:  # fmt: skip
    r = client.post("/form/skmei-tn", data=form(**over))
    assert r.status_code == 400
    assert field_msg in r.text
    assert count_orders(db_session) == 0
    if over.get("name") is None:
        assert "Mariem Gharbi" in r.text  # values kept


def test_honeypot_stores_nothing(client: TestClient, db_session: Session, shop: Any) -> None:
    r = client.post("/form/skmei-tn", data=form(website="http://spam"), follow_redirects=False)
    assert r.status_code == 303
    assert count_orders(db_session) == 0


def test_html_is_escaped(client: TestClient, shop: Any) -> None:
    r = client.post("/form/skmei-tn", data=form(name="<script>alert(1)</script>", phone="1"))
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_free_text_product_when_no_catalog(client: TestClient, db_session: Session) -> None:
    make_shop(db_session, slug="no-catalog", default_language=Language.FRENCH)
    db_session.commit()
    page = client.get("/form/no-catalog")
    assert 'name="product_text"' in page.text
    assert "Commander maintenant" in page.text
    r = client.post("/form/no-catalog",
                    data=form("no-catalog", product="", product_text="Montre bleue", lang="fr"),
                    follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    order = db_session.scalars(select(Order)).one()
    assert order.items[0]["name"] == "Montre bleue"
    assert order.total == Decimal("0.000")


@pytest.mark.parametrize("token", [None, "garbage", "other-shop"])
def test_form_token_required(
    client: TestClient, db_session: Session, shop: Any, token: str | None
) -> None:
    data = form()
    if token is None:
        data.pop("ft")
    elif token == "other-shop":
        data["ft"] = make_form_token("another-slug")
    else:
        data["ft"] = token
    r = client.post("/form/skmei-tn", data=data)
    assert r.status_code == 400
    assert count_orders(db_session) == 0
    assert 'name="ft" value="' in r.text  # a fresh token so the customer can retry


def test_expired_form_token(client: TestClient, db_session: Session, shop: Any,
                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    import app.security as sec

    data = form()
    monkeypatch.setattr(sec, "FORM_TOKEN_MAX_AGE", -1)
    assert client.post("/form/skmei-tn", data=data).status_code == 400
