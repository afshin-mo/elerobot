from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import Currency, InvoiceKind, InvoiceStatus


class Invoice(Base, IdMixin, TenantMixin, TimestampMixin):
    """An itemized invoice sent to a customer as a message (no payment gateway). Issued
    invoices are never edited: a wrong one is cancelled and a new one issued."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_invoices_number"),
        UniqueConstraint("company_id", "year", "seq", name="uq_invoices_year_seq"),
        CheckConstraint("total > 0 AND discount >= 0 AND tax_amount >= 0", name="amounts"),
        CheckConstraint("tax_percent >= 0 AND tax_percent <= 100", name="tax_percent"),
    )

    number: Mapped[str] = mapped_column(String(20))  # for example 2026-0001
    year: Mapped[int] = mapped_column(SmallInteger)
    seq: Mapped[int] = mapped_column(Integer)  # yearly counter per company
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"), index=True)
    kind: Mapped[InvoiceKind] = mapped_column(enum_col(InvoiceKind, "kind"))
    issue_date: Mapped[date] = mapped_column(Date, index=True)
    currency: Mapped[Currency] = mapped_column(enum_col(Currency, "currency"))
    subtotal: Mapped[int] = mapped_column(BigInteger)
    discount: Mapped[int] = mapped_column(BigInteger, default=0)
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    tax_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    total: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[InvoiceStatus] = mapped_column(
        enum_col(InvoiceStatus, "status"), default=InvoiceStatus.ISSUED, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceItem(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "invoice_items"
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0", name="amounts"),)

    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(BigInteger)


class InvoiceCounter(Base, IdMixin, TenantMixin, TimestampMixin):
    """Last invoice number used per company and year (locked while a number is taken)."""

    __tablename__ = "invoice_counters"
    __table_args__ = (UniqueConstraint("company_id", "year", name="uq_invoice_counters_year"),)

    year: Mapped[int] = mapped_column(SmallInteger)
    last_number: Mapped[int] = mapped_column(Integer, default=0)
