"""The invoice as a formatted message. No Telegram code: only texts and numbers."""
from datetime import date
from decimal import Decimal
from typing import Sequence

from app.core.i18n import t
from app.services.invoice_math import Totals

LRM = "\u200e"  # keeps numbers and dates readable inside right-to-left text


def money(value: int) -> str:
    return f"{value:,}"


def percent_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def render(
    lang: str, *, company_name: str, number: str | None, issue_date: date | None, customer: str,
    building: str, kind: str, currency: str, items: Sequence[tuple[str, int, int]],
    totals: Totals | None, notes: str | None, cancelled: bool = False,
) -> str:
    """number and issue_date are None for a draft (not issued yet); totals is None when the
    amounts cannot be computed yet."""
    cur = t(lang, f"cur_{currency}")
    lines = [company_name, t(lang, "inv_title", number=number) if number else t(lang, "inv_title_draft")]
    if cancelled:
        lines.append(t(lang, "inv_cancelled"))
    if issue_date is not None:
        lines.append(f"{t(lang, 'inv_date')}: {LRM}{issue_date.isoformat()}{LRM}")
    lines += [
        f"{t(lang, 'inv_customer')}: {customer}",
        f"{t(lang, 'label_building')}: {building}",
        f"{t(lang, 'inv_kind')}: {t(lang, f'kind_{kind}')}",
        f"{t(lang, 'inv_currency')}: {cur}",
        "",
        f"{t(lang, 'inv_items')}:",
    ]
    if not items:
        lines.append(t(lang, "inv_no_items"))
    for index, (description, quantity, price) in enumerate(items, start=1):
        lines.append(f"{index}. {description}")
        lines.append(f"   {LRM}{quantity} × {money(price)} = {money(quantity * price)}{LRM}")
    if totals is not None:
        lines.append("")
        lines.append(f"{t(lang, 'inv_subtotal')}: {LRM}{money(totals.subtotal)}{LRM}")
        if totals.discount:
            lines.append(f"{t(lang, 'inv_discount')}: {LRM}-{money(totals.discount)}{LRM}")
        if totals.tax_amount or totals.tax_percent:
            lines.append(
                f"{t(lang, 'inv_tax', percent=percent_text(totals.tax_percent))}: {LRM}{money(totals.tax_amount)}{LRM}"
            )
        lines.append(f"{t(lang, 'inv_total')}: {LRM}{money(totals.total)} {cur}{LRM}")
    if notes:
        lines += ["", f"{t(lang, 'label_notes')}: {notes}"]
    return "\n".join(lines)
