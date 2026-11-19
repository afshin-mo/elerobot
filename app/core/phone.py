import re

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NON_DIGITS = re.compile(r"\D", re.ASCII)


def normalize_phone(raw: str) -> str | None:
    """Return the number as '+<country code><digits>', or None if it is not valid.

    Accepted: international ('+964...', '964...', '00964...') and Iraqi local mobile
    ('07xx xxx xxxx'). Persian/Arabic digits and separators are handled.
    Other local formats are rejected: they must be written with the country code.
    """
    if not raw or len(raw) > 30:
        return None
    digits = _NON_DIGITS.sub("", raw.translate(_DIGIT_MAP))
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("07") and len(digits) == 11:
        digits = "964" + digits[1:]
    elif digits.startswith("0"):
        return None
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits
