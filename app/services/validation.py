"""Field rules shared by services (final check) and the bot (early feedback per step)."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from app.core.phone import normalize_phone
from app.core.timeutil import LOCAL_TZ
from app.models import RepeatUnit
from app.services.errors import InvalidInput


class Field(NamedTuple):
    kind: str  # "text" | "phone" | "date" | "datetime" | "repeat" | "int" | "percent" | "discount"
    max_len: int
    required: bool
    short: str  # model attribute name and i18n suffix (prompt_<short>, btn_edit_<short>)


FIELDS = {
    "user_name": Field("text", 200, True, "name"),
    "phone": Field("phone", 20, True, "phone"),
    "building_name": Field("text", 200, True, "name"),
    "address": Field("text", 500, True, "address"),
    "notes": Field("text", 1000, False, "notes"),
    "elevator_name": Field("text", 100, True, "name"),
    "brand": Field("text", 100, False, "brand"),
    "install_date": Field("date", 10, False, "install_date"),
    "description": Field("text", 2000, True, "description"),
    "report": Field("text", 2000, True, "report"),
    "note": Field("text", 1000, True, "note"),
    "comment": Field("text", 1000, False, "comment"),
    "lead_message": Field("text", 1000, True, "lead_message"),
    "start_date": Field("date", 10, True, "start_date"),
    "end_date": Field("date", 10, True, "end_date"),
    "contract_notes": Field("text", 1000, False, "notes"),
    "visit_time": Field("datetime", 16, True, "visit_time"),
    "visit_repeat": Field("repeat", 5, False, "visit_repeat"),
    "change_note": Field("text", 500, False, "change_note"),
    "item_desc": Field("text", 200, True, "item_desc"),
    "item_qty": Field("int", 6, True, "item_qty"),
    "item_price": Field("int", 12, True, "item_price"),
    "invoice_notes": Field("text", 1000, False, "invoice_notes"),
    "discount": Field("discount", 14, False, "discount"),
    "tax": Field("percent", 6, False, "tax"),
}

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬،", "01234567890123456789.,,")


def _number_text(value: str) -> str:
    """ASCII digits; thousands separators and spaces removed (1,000,000 / ۱٬۰۰۰ / 1 000)."""
    return value.translate(_DIGITS).replace(",", "").replace(" ", "")


def _whole_number(value: str, max_digits: int) -> int:
    text = _number_text(value)
    if not text.isascii() or not text.isdigit() or len(text) > max_digits:
        raise InvalidInput("err_invalid_number")
    number = int(text)
    if number < 1:
        raise InvalidInput("err_invalid_number")
    return number


def _percent(text: str) -> Decimal:
    try:
        percent = Decimal(text.translate(_DIGITS).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        raise InvalidInput("err_invalid_percent") from None
    if not percent.is_finite() or not 0 <= percent <= 100 or percent != percent.quantize(Decimal("0.01")):
        raise InvalidInput("err_invalid_percent")
    return percent


def validate_field(field: str, raw: str | None) -> str | date | None:
    """Return the cleaned value; None means 'empty' (optional fields only: '-' skips)."""
    spec = FIELDS[field]
    value = (raw or "").strip()
    if not value or value == "-":
        if spec.required:
            raise InvalidInput("err_invalid_text")
        return None
    if spec.kind == "phone":
        phone = normalize_phone(value)
        if phone is None:
            raise InvalidInput("invalid_phone")
        return phone
    if spec.kind == "date":
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise InvalidInput("err_invalid_date") from None
    if spec.kind == "datetime":  # local company time, for example 2026-12-01 09:30
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        except ValueError:
            raise InvalidInput("err_invalid_datetime") from None
    if spec.kind == "int":  # a whole number of at least 1
        return _whole_number(value, spec.max_len)
    if spec.kind == "percent":  # tax: 0 to 100 with at most two decimals
        return _percent(value)
    if spec.kind == "discount":  # an amount ("50000") or a percent ("10%")
        if value.endswith("%"):
            percent = _percent(value[:-1].strip())
            if not 0 < percent < 100:
                raise InvalidInput("err_invalid_discount")
            return ("percent", percent)
        try:
            return ("amount", _whole_number(value, spec.max_len))
        except InvalidInput:
            raise InvalidInput("err_invalid_discount") from None
    if spec.kind == "repeat":  # 30d = every 30 days, 3m = every 3 months
        match = re.fullmatch(r"(\d{1,3})([dm])", value.lower())
        if match is None:
            raise InvalidInput("err_invalid_repeat")
        count, unit = int(match.group(1)), RepeatUnit.DAYS if match.group(2) == "d" else RepeatUnit.MONTHS
        if not 1 <= count <= (365 if unit is RepeatUnit.DAYS else 24):
            raise InvalidInput("err_invalid_repeat")
        return count, unit
    if len(value) > spec.max_len:
        raise InvalidInput("err_text_too_long", max=spec.max_len)
    return value


def validate_rating(value: int | str) -> int:
    """Feedback rating: a whole number from 1 to 5."""
    try:
        rating = int(str(value).strip())
    except ValueError:
        raise InvalidInput("err_invalid_rating") from None
    if not 1 <= rating <= 5:
        raise InvalidInput("err_invalid_rating")
    return rating
