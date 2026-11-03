from __future__ import annotations

import pytest

from app.services.intent import Intent, detect, detect_rules

C, M, X, S, Q, U, ST = (Intent.CONFIRM, Intent.MODIFY, Intent.CANCEL, Intent.STOP,
                        Intent.QUESTION, Intent.UNKNOWN, Intent.START)  # fmt: skip


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- Darija (Latin)
        ("ey", C), ("Eyyyy", C), ("ey nconfirmi", C), ("behi", C), ("mrigel", C),
        ("Mriguel ya3tik essa7a", C), ("ok", C), ("okkk merci", C), ("aywa", C),
        ("ey n7eb", C), ("nconfirmi l commande", C), ("1", C), ("akked", C),
        ("la", X), ("laaa", X), ("la ma n7ebch", X), ("ma n7ebch", X), ("man7ebch", X),
        ("nannuli", X), ("annuli l commande", X), ("ma 7ajtich biha", X), ("3", X),
        ("bdalt ra2yi", X), ("ma nconfirmich", X), ("manconfirmich", X),
        ("n7eb nbaddel l adresse", M), ("badel", M), ("nbadel l 9yas", M), ("2", M),
        ("ey ama badel l lon", M), ("l adresse ghalta", M),
        ("ma t3awedch tab3athli", S), ("STOP", S), ("stop", S), ("ma tab3thouli chay", S),
        ("start", ST),
        ("9adech l livraison?", Q), ("waktech tousel", Q), ("win l boutique", Q),
        ("salam", U), ("👋", U), ("", U), ("ahla", U),
        # --- French
        ("Oui", C), ("oui je confirme", C), ("Je confirme la commande", C), ("d'accord", C),
        ("parfait merci", C), ("Non", X), ("non merci", X), ("j'annule", X),
        ("annulez svp", X), ("je ne veux plus", X), ("pas besoin", X),
        ("je veux changer la taille", M), ("modifier l'adresse svp", M),
        ("Je confirme pas", X), ("arrêtez", U), ("arrête", S), ("se désabonner", S),
        ("c'est quand la livraison ?", Q), ("combien de jours", Q),
        # --- Arabic
        ("نعم", C), ("نعم أكيد", C), ("موافق", C), ("ايه", C), ("لا", X), ("نلغي", X),
        ("ما نحبش", X), ("نحب نبدل العنوان", M), ("تعديل", M), ("توقف", S), ("لا ترسل", S),
        ("قداش التوصيل", Q), ("متى يصل؟", Q), ("١", C), ("٣", X),
        # --- Emojis
        ("✅", C), ("👍", C), ("👍👍", C), ("❌", X), ("👎", X), ("✏️", M),
        # --- Ambiguous / mixed
        ("ey la", U), ("oui non", U),
    ],
)  # fmt: skip
def test_rules(text: str, expected: Intent) -> None:
    assert detect_rules(text) is expected, text


def test_weak_words_inside_sentences() -> None:
    # "la" here is the French article, not "no"
    assert detect_rules("je confirme la commande de la montre") is C
    assert detect_rules("ok mais je veux changer la couleur") is M


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("CONFIRM:12", C), ("MODIFY:12", M), ("CANCEL:12", X), ("cancel:3", X), ("WHATEVER:1", U)],
)
def test_payload_wins(payload: str, expected: Intent) -> None:
    assert detect_rules("", payload) is expected


def test_payload_beats_text() -> None:
    assert detect_rules("la", "CONFIRM:1") is C


class FakeLLM:
    def __init__(self, answer: Intent | None) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def classify(self, text: str) -> Intent | None:
        self.calls.append(text)
        return self.answer


def test_llm_only_used_when_rules_unknown() -> None:
    llm = FakeLLM(Intent.CONFIRM)
    assert detect("ey", classifier=llm) is C
    assert llm.calls == []
    assert detect("inchallah rabi ysahel", classifier=llm) is C
    assert llm.calls == ["inchallah rabi ysahel"]


def test_llm_failure_falls_back_to_unknown() -> None:
    assert detect("hmmm", classifier=FakeLLM(None)) is U
    assert detect("hmmm") is U  # no classifier configured
