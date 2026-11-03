"""Tunisian phone number normalization to E.164 (+216XXXXXXXX).

Accepted inputs (spaces, dots, dashes, parentheses ignored, Arabic-Indic digits ok):
    98123456 / 98 123 456 / +216 98 123 456 / 0021698123456 / 21698123456 / ٩٨١٢٣٤٥٦

Only mobile numbers are accepted, since customers are contacted on WhatsApp:
first digit 2 (Ooredoo), 4 (Lyca / TT), 5 (Orange), 9 (Tunisie Telecom).
Landlines (7x) and other countries are rejected for now.
"""

from __future__ import annotations

import re

TN_COUNTRY_CODE = "216"
TN_MOBILE_FIRST_DIGITS = frozenset("2459")

# Arabic-Indic (٠-٩) and Eastern Arabic / Persian (۰-۹) digits -> ASCII.
_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SEPARATORS = re.compile(r"[\s.\-()/‎‏‪-‮]")


class InvalidPhone(ValueError):
    """The phone number is not a valid Tunisian mobile number."""


def normalize_phone(raw: str) -> str:
    """Return the number as E.164 ('+21698123456') or raise `InvalidPhone`."""
    if not raw or len(raw) > 32:
        raise InvalidPhone("empty or too long")

    s = _SEPARATORS.sub("", raw.translate(_DIGIT_MAP))

    if s.startswith("+"):
        s = s[1:]
        if not s.startswith(TN_COUNTRY_CODE):
            raise InvalidPhone("only Tunisian numbers (+216) are supported")
        s = s[len(TN_COUNTRY_CODE) :]
    elif s.startswith("00" + TN_COUNTRY_CODE):
        s = s[2 + len(TN_COUNTRY_CODE) :]
    elif s.startswith(TN_COUNTRY_CODE) and len(s) == len(TN_COUNTRY_CODE) + 8:
        s = s[len(TN_COUNTRY_CODE) :]

    if not s.isdigit() or len(s) != 8:
        raise InvalidPhone("a Tunisian number has 8 digits")
    if s[0] not in TN_MOBILE_FIRST_DIGITS:
        raise InvalidPhone("not a Tunisian mobile number")
    return f"+{TN_COUNTRY_CODE}{s}"
