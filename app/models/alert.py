from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import AlertStatus


class EmergencyAlert(Base, IdMixin, TenantMixin, TimestampMixin):
    """An alert sent by scanning an elevator's QR code (no sign-in needed). It becomes a ticket
    only after a manager confirms."""

    __tablename__ = "emergency_alerts"

    elevator_id: Mapped[int] = mapped_column(ForeignKey("elevators.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)  # who scanned
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[AlertStatus] = mapped_column(
        enum_col(AlertStatus, "status"), default=AlertStatus.NEW, index=True
    )
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"))
    handled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
