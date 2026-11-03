from __future__ import annotations

import pytest

from app.services.phone import InvalidPhone, normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "98123456",
        "98 123 456",
        "98.123.456",
        "98-123-456",
        "+216 98 123 456",
        "+21698123456",
        "0021698123456",
        "00216 98 123 456",
        "21698123456",
        "(+216) 98-123-456",
        "٩٨١٢٣٤٥٦",  # Arabic-Indic digits
        "‎+216 98 123 456",  # invisible LTR mark (copy-paste from phones)
    ],
)
def test_valid_formats(raw: str) -> None:
    assert normalize_phone(raw) == "+21698123456"


@pytest.mark.parametrize("first", ["2", "4", "5", "9"])
def test_mobile_prefixes(first: str) -> None:
    assert normalize_phone(f"{first}1234567") == f"+216{first}1234567"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "1234",
        "981234567",  # 9 digits
        "9812345",  # 7 digits
        "71123456",  # landline
        "31123456",
        "+33612345678",  # France
        "+212612345678",  # Morocco (not yet)
        "98abc456",
        "98123456" * 5,  # too long
        "+",
    ],
)
def test_invalid(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)
