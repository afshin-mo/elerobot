from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, SmallInteger, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import ContractStatus


class Contract(Base, IdMixin, TenantMixin, TimestampMixin):
    """Service contract of one building (covers all its elevators). The party is the
    building's owner (a customer)."""

    __tablename__ = "contracts"
    __table_args__ = (CheckConstraint("end_date >= start_date", name="dates_order"),)

    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[ContractStatus] = mapped_column(
        enum_col(ContractStatus, "status"), default=ContractStatus.ACTIVE, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    renewal_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContractReminder(Base, IdMixin, TenantMixin, TimestampMixin):
    """One row per expiry reminder already sent, so no reminder is sent twice."""

    __tablename__ = "contract_reminders"
    __table_args__ = (UniqueConstraint("contract_id", "days_before", name="uq_contract_reminders_step"),)

    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"), index=True)
    days_before: Mapped[int] = mapped_column(SmallInteger)  # 30, 15 or 7
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
