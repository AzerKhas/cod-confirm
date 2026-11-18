"""Guess the language of a customer's WhatsApp message, so the bot answers in it.

    Arabic script            -> Arabic (Modern Standard, understood everywhere; Tunisian
                                dialect written in Arabic letters gets MSA replies too)
    Latin + Tunisian markers -> Tunisian Darija ("n7eb", "3andi", "9adech", "ey", "behi"...)
    Latin French words       -> French
    Latin English words      -> English

Darija wins over French when both appear ("nheb nbadel l'adresse" is Darija even with a
French word). Short or neutral replies ("ok", "1", "👍") return None: the language does not
change, so one emoji never flips a French customer to another language.
"""

from __future__ import annotations

import re

from app.enums import Language
from app.services.intent import canon, tokens

_ARABIC = range(0x0600, 0x0700)
# A Latin word containing the digits used for Arabic sounds: 3 (ع), 7 (ح), 9 (ق), 5 (خ), 2 (ء).
_ARABIZI = re.compile(r"^(?=.*[a-z])(?=.*[23579])[a-z0-9]+$")


def _set(*words: str) -> frozenset[str]:
    return frozenset(canon(w) for w in words)


DARIJA = _set(
    "ey", "eyh", "behi", "bahi", "mrigel", "mriguel", "chnowa", "chnia", "kifech", "nheb", "mte3i",
    "mte3ek", "barcha", "yezzi", "taw", "bech", "fama", "famma", "ena", "enti", "inti", "chkoun",
    "lech", "aleh", "zeda", "zada", "brabi", "mouch", "mch", "mich", "ma3adch", "nchallah",
    "inchallah", "sahit", "aychek", "yaychek", "hedhi", "hedha", "kifeh", "wakteh", "waktech",
)  # fmt: skip
FRENCH = _set(
    "je", "oui", "non", "bonjour", "bonsoir", "commande", "annuler", "annule", "modifier",
    "adresse", "svp", "pas", "veux", "voudrais", "est", "suis", "les", "des", "du", "une", "mon",
    "votre",
    "livraison", "quand", "combien", "pourquoi", "avec", "pour", "taille", "couleur", "confirme",
    "accord", "merci", "vous", "nous", "aussi", "encore", "comment", "changer",
)  # fmt: skip
ENGLISH = _set(
    "yes", "yeah", "yep", "the", "i", "my", "please", "thanks", "thank", "you", "want", "change",
    "cancel", "order", "address", "is", "it", "when", "how", "what", "hello", "hi", "delivery",
    "size", "color", "colour", "confirm", "dont", "not", "will", "can", "need", "would", "sure",
    "where", "why", "much", "wrong", "shipping", "deliver",
)  # fmt: skip


def detect_language(text: str) -> Language | None:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    if sum(ord(c) in _ARABIC for c in letters) / len(letters) > 0.5:
        return Language.ARABIC

    toks = tokens(text)
    darija = sum(1 for t in toks if t in DARIJA or _ARABIZI.match(t))
    if darija:
        return Language.DARIJA
    french = sum(1 for t in toks if t in FRENCH)
    english = sum(1 for t in toks if t in ENGLISH)
    if french > english:
        return Language.FRENCH
    if english > french:
        return Language.ENGLISH
    return None
