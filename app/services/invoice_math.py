"""Invoice amounts. Whole numbers only (dinar, rial and toman have no useful decimals);
percentages are rounded half up."""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from app.services.errors import InvalidInput


@dataclass(frozen=True)
class Totals:
    subtotal: int
    discount: int
    tax_percent: Decimal
    tax_amount: int
    total: int


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def compute_totals(
    items: Iterable[tuple[int, int]],
    discount: tuple[str, int | Decimal] | None = None,
    tax_percent: Decimal | None = None,
) -> Totals:
    """items: (quantity, unit price). discount: ("amount", n) or ("percent", p) or None.
    The discount comes off the subtotal first; tax is charged on what is left."""
    subtotal = sum(quantity * price for quantity, price in items)
    if discount is None:
        off = 0
    elif discount[0] == "amount":
        off = int(discount[1])
    else:
        off = _round(Decimal(subtotal) * Decimal(discount[1]) / 100)
    if subtotal <= 0 or off >= subtotal:
        raise InvalidInput("err_invoice_total")  # the total must stay above zero
    rate = tax_percent or Decimal(0)
    taxable = subtotal - off
    tax = _round(Decimal(taxable) * rate / 100)
    return Totals(subtotal, off, rate, tax, taxable + tax)
