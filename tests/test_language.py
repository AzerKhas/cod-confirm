from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.enums import Language, OrderStatus
from app.services.intent import Intent, detect_rules
from app.services.language import detect_language
from tests.factories import make_customer, make_order, make_shop
from tests.test_conversation import FakeClient, say

A, D, F, E = Language.ARABIC, Language.DARIJA, Language.FRENCH, Language.ENGLISH


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Arabic script (MSA or Tunisian written in Arabic letters) -> Arabic replies
        ("نعم أكيد", A), ("نحب نبدل العنوان", A), ("شكرا", A),
        # Tunisian Darija in Latin letters
        ("ey nconfirmi", D), ("n7eb nbaddel l adresse", D), ("9adech l livraison", D),
        ("behi", D), ("nheb nbadel l'adresse svp", D),  # Darija wins over French words
        # French
        ("Oui je confirme", F), ("je veux annuler la commande", F), ("bonjour, quand la livraison ?", F),
        # English
        ("Yes I confirm", E), ("I want to change the address", E), ("please cancel my order", E),
        ("yes", E), ("hello, when is the delivery?", E),
        # Too short / neutral: keep the current language
        ("ok", None), ("👍", None), ("1", None), ("", None), ("Mohamed", None),
    ],
)  # fmt: skip
def test_detect_language(text: str, expected: Language | None) -> None:
    assert detect_language(text) is expected


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("yes", Intent.CONFIRM), ("Yes I confirm", Intent.CONFIRM), ("sure", Intent.CONFIRM),
        ("yeah ok", Intent.CONFIRM), ("no", Intent.CANCEL), ("cancel my order", Intent.CANCEL),
        ("I don't want it anymore", Intent.CANCEL), ("no thanks", Intent.CANCEL),
        ("I don't confirm", Intent.CANCEL), ("I want to change the size", Intent.MODIFY),
        ("wrong address", Intent.MODIFY), ("yes but change the colour", Intent.MODIFY),
        ("when will it arrive?", Intent.QUESTION), ("STOP", Intent.STOP), ("unsubscribe", Intent.STOP),
    ],
)  # fmt: skip
def test_english_intents(text: str, intent: Intent) -> None:
    assert detect_rules(text) is intent


def _world(db: Session, language: Language | None) -> object:
    shop = make_shop(db, default_language=Language.DARIJA, whatsapp_phone_number_id="PN-L")
    customer = make_customer(
        db, shop, name="John Smith", phone_e164="+21698123456", language=language
    )
    make_order(db, shop, customer, status=OrderStatus.AWAITING_CONFIRMATION)
    db.commit()
    return shop


def test_english_customer_gets_english_replies(db_session: Session) -> None:
    shop = _world(db_session, None)
    _i, client = say(db_session, shop, "Yes I confirm")  # type: ignore[arg-type]
    assert client.texts[0].startswith("Thank you John ✅")


def test_language_follows_the_customer(db_session: Session) -> None:
    shop = _world(db_session, Language.FRENCH)  # ordered on the French form
    _i, c1 = say(db_session, shop, "what is the delivery time?")  # type: ignore[arg-type]
    assert c1.texts[0].startswith("Your question has been sent")
    _i, c2 = say(db_session, shop, "ok")  # neutral word: the conversation stays in English
    assert c2.texts[0].startswith("Thank you John ✅")


def test_button_reply_keeps_language(db_session: Session) -> None:
    shop = _world(db_session, Language.ENGLISH)
    client = FakeClient()
    _i, client = say(db_session, shop, "✅ Nconfirmi", payload="CONFIRM:1", client=client)  # type: ignore[arg-type]
    assert client.texts[0].startswith("Thank you")
