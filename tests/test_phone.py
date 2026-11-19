import pytest

from app.core.phone import normalize_phone


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+964 750 123 4567", "+9647501234567"),
        ("9647501234567", "+9647501234567"),  # how Telegram sends contacts
        ("00964-750-123-4567", "+9647501234567"),
        ("0750 123 4567", "+9647501234567"),  # Iraqi local mobile
        ("۰۷۵۰ ۱۲۳ ۴۵۶۷", "+9647501234567"),  # Persian digits
        ("٠٧٥٠١٢٣٤٥٦٧", "+9647501234567"),  # Arabic-Indic digits
        ("+98 912 345 6789", "+989123456789"),
    ],
)
def test_valid_numbers(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "123", "09123456789", "+" + "9" * 16, "1" * 40])
def test_invalid_numbers(raw):
    assert normalize_phone(raw) is None
