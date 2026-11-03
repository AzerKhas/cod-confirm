from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.config import Settings
from app.crypto import encrypt_secret
from app.enums import Language, MessageStatus, OrderStatus
from app.services import messages as m
from app.services.notify import send_order_confirmation, send_order_template
from app.services.whatsapp import WhatsAppClient, client_for_shop
from tests.factories import make_customer, make_order, make_shop

VAR = re.compile(r"\{\{(\d+)\}\}")


# ------------------------------------------------------------- template rules
@pytest.mark.parametrize("tpl", m.TEMPLATES, ids=lambda t: t.key)
@pytest.mark.parametrize("lang", list(Language))
def test_templates_follow_meta_rules(tpl: m.WaTemplate, lang: Language) -> None:
    body = tpl.bodies[lang]
    nums = [int(n) for n in VAR.findall(body)]
    assert nums == list(range(1, len(tpl.variables) + 1))  # {{1}}..{{n}} in order
    assert len(tpl.samples) == len(tpl.variables)
    assert not body.startswith("{{") and not body.rstrip().endswith("}}")
    assert "}}{{" not in body.replace(" ", "")
    assert len(body) <= 1024
    assert len(tpl.footers.get(lang, "")) <= 60
    for b in tpl.buttons.get(lang, ()):
        assert len(b) <= 25
    assert re.fullmatch(r"[a-z0-9_]+", tpl.name(lang))


def test_template_names() -> None:
    assert m.ORDER_CONFIRMATION.name(Language.DARIJA) == "cod_order_confirmation_tn"
    assert m.ORDER_SHIPPED.name(Language.ARABIC) == "cod_order_shipped_ar"


@pytest.mark.parametrize(
    ("amount", "text"), [("118", "118"), ("89.5", "89.500"), ("0.250", "0.250"), ("59.000", "59")]
)
def test_format_amount(amount: str, text: str) -> None:
    assert m.format_amount(Decimal(amount)) == text


def test_params_are_cleaned() -> None:
    assert m.clean_param("a\n\tb    c") == "a b c"
    assert m.clean_param("") == "-"
    assert len(m.clean_param("x" * 300)) == 120
    assert m.first_name("  Ahmed Ben Salah ") == "Ahmed"
    items = [{"name": "Robe", "qty": 2}, {"name": "Foulard\nrouge", "qty": 1}]
    assert m.items_summary(items) == "2× Robe, 1× Foulard rouge"


# ------------------------------------------------------------------ sending
def test_confirmation_dry_run(db_session: Session) -> None:
    shop = make_shop(db_session, name="Boutique Yasmine")
    order = make_order(db_session, shop, items=[{"name": "Robe", "qty": 1, "price": "45.000"}],
                       total=Decimal("45"), city="Sousse")  # fmt: skip
    msg = send_order_confirmation(db_session, order)
    assert msg is not None
    assert msg.status is MessageStatus.SENT
    assert msg.wa_message_id.startswith("dryrun.")  # type: ignore[union-attr]
    assert "Boutique Yasmine" in msg.content and "45 DT" in msg.content
    assert order.status is OrderStatus.AWAITING_CONFIRMATION
    assert order.last_contacted_at is not None

    assert send_order_confirmation(db_session, order) is None  # only once


def test_language_per_customer(db_session: Session) -> None:
    shop = make_shop(db_session, default_language=Language.FRENCH)
    ar_customer = make_customer(db_session, shop, language=Language.ARABIC)
    msg = send_order_template(
        db_session, make_order(db_session, shop, ar_customer), m.ORDER_CONFIRMATION
    )
    assert msg is not None and msg.content.startswith("مرحبا")
    msg_fr = send_order_template(db_session, make_order(db_session, shop), m.ORDER_CONFIRMATION)
    assert msg_fr is not None and msg_fr.content.startswith("Bonjour")


def test_opted_out_never_contacted(db_session: Session) -> None:
    shop = make_shop(db_session)
    customer = make_customer(db_session, shop, opted_out=True)
    order = make_order(db_session, shop, customer)
    assert send_order_confirmation(db_session, order) is None
    assert order.status is OrderStatus.NEW
    assert order.messages == []


@respx.mock
def test_failure_keeps_order_new(db_session: Session) -> None:
    respx.post(url__regex=r".*/messages").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": 132001, "message": "Template does not exist"}}
        )
    )
    shop = make_shop(db_session, whatsapp_phone_number_id="999",
                     access_token_encrypted=encrypt_secret("tok"))  # fmt: skip
    order = make_order(db_session, shop)
    client = client_for_shop(shop, Settings(dry_run=False, _env_file=None))  # type: ignore[arg-type]
    msg = send_order_confirmation(db_session, order, client)
    assert msg is not None and msg.status is MessageStatus.FAILED
    assert "132001" in (msg.error or "")
    assert order.status is OrderStatus.NEW  # the scheduler will retry


@respx.mock
def test_real_send_uses_decrypted_token(db_session: Session) -> None:
    route = respx.post("https://graph.facebook.com/v21.0/999/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.REAL"}]})
    )
    shop = make_shop(db_session, whatsapp_phone_number_id="999",
                     access_token_encrypted=encrypt_secret("tok-123"))  # fmt: skip
    order = make_order(db_session, shop)
    client = client_for_shop(shop, Settings(dry_run=False, _env_file=None))  # type: ignore[arg-type]
    assert isinstance(client, WhatsAppClient)
    msg = send_order_confirmation(db_session, order, client)
    assert msg is not None and msg.wa_message_id == "wamid.REAL"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-123"
    assert shop.access_token_encrypted != "tok-123"


def test_templates_doc_is_up_to_date() -> None:
    from scripts.export_templates import OUT, render

    assert OUT.read_text(encoding="utf-8") == render(), "run: python -m scripts.export_templates"
