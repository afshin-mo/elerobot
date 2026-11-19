"""Digital invoices (FR-15): itemized, numbered per year, sent to the customer as a message.
No payment gateway and no payment link (D-015). Issued invoices are never edited: a wrong one
is cancelled (the customer is told) and a new one issued."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.core.timeutil import local_today
from app.models import (
    Building, Company, Currency, Invoice, InvoiceItem, InvoiceKind, InvoiceStatus, Role, User,
)
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.invoices import InvoiceRepository
from app.repositories.users import UserRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.invoice_format import render
from app.services.invoice_math import Totals, compute_totals
from app.services.permissions import require_role
from app.services.validation import validate_field

log = logging.getLogger(__name__)

MAX_ITEMS = 20


@dataclass
class InvoiceDetails:
    invoice: Invoice
    items: list[InvoiceItem]
    customer: User
    building: Building


@dataclass
class IssueResult:
    invoice: Invoice
    queued: bool  # False when the customer cannot receive messages (never signed in)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enum(enum_cls, raw):
    try:
        return enum_cls(raw)
    except ValueError:
        raise InvalidInput() from None


class InvoiceService:
    def __init__(
        self, session: AsyncSession, company: Company, actor: User | None,
        outbox: Outbox | None = None,
    ):
        self.actor = require_role(actor, Role.MANAGER, Role.CUSTOMER)
        self.company = company
        self.outbox = outbox  # tests pass one; the bot uses the per-update outbox
        self.invoices = InvoiceRepository(session)
        self.users = UserRepository(session)
        self.buildings = BuildingRepository(session)
        self.audit = AuditRepository(session)

    @property
    def _is_manager(self) -> bool:
        return self.actor.role is Role.MANAGER

    def require_manager(self) -> None:
        if not self._is_manager:
            raise PermissionDenied()

    # ---- default currency (company setting, changeable by any manager) ----

    def default_currency(self) -> Currency | None:
        self.require_manager()
        raw = (self.company.settings or {}).get("default_currency")
        try:
            return Currency(raw) if raw else None
        except ValueError:
            return None

    async def set_default_currency(self, code: str) -> Currency:
        self.require_manager()
        currency = _enum(Currency, code)
        old = (self.company.settings or {}).get("default_currency")
        self.company.settings = {**(self.company.settings or {}), "default_currency": currency.value}
        await self.audit.add(
            self.company.id, "invoice_default_currency_changed", actor_id=self.actor.id,
            entity_type="company", entity_id=self.company.id,
            details={"from": old, "to": currency.value},
        )
        return currency

    # ---- reading ----

    async def list_invoices(self, page: int) -> tuple[list[tuple[Invoice, User]], int]:
        """Managers: all invoices. Customers: their own."""
        owner_id = None if self._is_manager else self.actor.id
        rows = await self.invoices.list_page(
            self.company.id, max(page, 0) * PAGE_SIZE, PAGE_SIZE, owner_id
        )
        return rows, await self.invoices.count(self.company.id, owner_id)

    async def get_details(self, invoice_id: int) -> InvoiceDetails:
        invoice = await self.invoices.get(self.company.id, invoice_id)
        if invoice is None or (not self._is_manager and invoice.customer_id != self.actor.id):
            raise NotFound()  # customers only see their own invoices
        customer = await self.users.get_by_id(self.company.id, invoice.customer_id)
        building = await self.buildings.get(self.company.id, invoice.building_id)
        return InvoiceDetails(invoice, await self.invoices.items(invoice.id), customer, building)

    def text(self, lang: str, d: InvoiceDetails) -> str:
        """The invoice as a message in the given language."""
        inv = d.invoice
        totals = Totals(inv.subtotal, inv.discount, inv.tax_percent, inv.tax_amount, inv.total)
        return render(
            lang, company_name=self.company.name, number=inv.number, issue_date=inv.issue_date,
            customer=d.customer.name, building=d.building.name, kind=inv.kind.value,
            currency=inv.currency.value,
            items=[(i.description, i.quantity, i.unit_price) for i in d.items],
            totals=totals, notes=inv.notes, cancelled=inv.status is InvoiceStatus.CANCELLED,
        )

    # ---- draft helpers (used by the bot while a manager builds an invoice) ----

    @staticmethod
    def draft_totals(items: list[tuple[str, str, str]], discount_raw: str | None, tax_raw: str | None) -> Totals:
        """Validate the draft's amounts and compute the totals. Raises InvalidInput."""
        parsed = [
            (validate_field("item_qty", qty), validate_field("item_price", price))
            for _, qty, price in items
        ]
        return compute_totals(parsed, validate_field("discount", discount_raw), validate_field("tax", tax_raw))

    # ---- manager actions ----

    async def issue(
        self, customer_id: int, building_id: int, kind_raw: str, currency_raw: str,
        items: list[tuple[str, str, str]], discount_raw: str | None = None,
        tax_raw: str | None = None, notes_raw: str | None = None,
    ) -> IssueResult:
        self.require_manager()
        customer = await self.users.get_by_id(self.company.id, customer_id)
        if customer is None or customer.role is not Role.CUSTOMER:
            raise NotFound()
        building = await self.buildings.get(self.company.id, building_id)
        if building is None or building.owner_id != customer.id:
            raise NotFound()
        kind = _enum(InvoiceKind, kind_raw)
        currency = _enum(Currency, currency_raw)
        if not 1 <= len(items) <= MAX_ITEMS:
            raise InvalidInput("err_invoice_items", max=MAX_ITEMS)
        clean = [
            (validate_field("item_desc", d), validate_field("item_qty", q), validate_field("item_price", p))
            for d, q, p in items
        ]
        discount = validate_field("discount", discount_raw)
        tax = validate_field("tax", tax_raw)
        notes = validate_field("invoice_notes", notes_raw)
        totals = compute_totals([(q, p) for _, q, p in clean], discount, tax)

        today = local_today()
        seq = await self.invoices.next_seq(self.company.id, today.year)
        invoice = await self.invoices.add(
            Invoice(
                company_id=self.company.id, number=f"{today.year}-{seq:04d}", year=today.year, seq=seq,
                customer_id=customer.id, building_id=building.id, kind=kind, issue_date=today,
                currency=currency, subtotal=totals.subtotal, discount=totals.discount,
                tax_percent=totals.tax_percent, tax_amount=totals.tax_amount, total=totals.total,
                status=InvoiceStatus.ISSUED, notes=notes,
            ),
            [
                InvoiceItem(company_id=self.company.id, position=i, description=d, quantity=q, unit_price=p)
                for i, (d, q, p) in enumerate(clean, start=1)
            ],
        )
        await self._log("invoice_issued", invoice, total=invoice.total, currency=currency.value)
        queued = await self._send(await self.get_details(invoice.id))
        return IssueResult(invoice, queued)

    async def resend(self, invoice_id: int) -> bool:
        """Send an issued invoice to its customer again. Returns False if they cannot receive it."""
        self.require_manager()
        details = await self.get_details(invoice_id)
        if details.invoice.status is not InvoiceStatus.ISSUED:
            raise InvalidInput("err_invoice_state")
        queued = await self._send(details)
        await self._log("invoice_resent", details.invoice, delivered=queued)
        return queued

    async def cancel(self, invoice_id: int) -> Invoice:
        self.require_manager()
        details = await self.get_details(invoice_id)
        invoice = details.invoice
        if invoice.status is not InvoiceStatus.ISSUED:
            raise InvalidInput("err_invoice_state")
        invoice.status = InvoiceStatus.CANCELLED
        await self._log("invoice_cancelled", invoice)
        customer = details.customer
        box = self.outbox or current_outbox.get()
        if box is not None and self._can_receive(customer):
            box.add(Notification(
                telegram_id=customer.telegram_id, language=customer.language or self.company.default_language,
                key="notif_invoice_cancelled", params={"number": invoice.number}, invoice_id=invoice.id,
            ))
        return invoice

    # ---- helpers ----

    @staticmethod
    def _can_receive(user: User | None) -> bool:
        return user is not None and user.is_active and bool(user.telegram_id)

    async def _send(self, d: InvoiceDetails) -> bool:
        box = self.outbox or current_outbox.get()
        if box is None or not self._can_receive(d.customer):
            return False
        lang = d.customer.language or self.company.default_language
        box.add(Notification(
            telegram_id=d.customer.telegram_id, language=lang, key="notif_invoice",
            params={"body": self.text(lang, d)}, invoice_id=d.invoice.id,
        ))
        d.invoice.sent_at = _now()
        return True

    async def _log(self, event_type: str, invoice: Invoice, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="invoice",
            entity_id=invoice.id, details={"number": invoice.number, **details},
        )
