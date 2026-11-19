from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, SmallInteger, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import ReviewStatus, TicketStatus

# The ticket's visible number is tickets.id (decision: no separate per-company counter yet).


class Ticket(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "tickets"

    elevator_id: Mapped[int] = mapped_column(ForeignKey("elevators.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        enum_col(TicketStatus, "status"), default=TicketStatus.NEW, index=True
    )
    technician_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class TicketAttachment(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ticket_attachments"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))


class TicketReport(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ticket_reports"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_col(ReviewStatus, "review_status"), default=ReviewStatus.PENDING
    )
    review_note: Mapped[str | None] = mapped_column(Text)  # manager's reason when rejecting
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketFeedback(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ticket_feedback"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),)

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), unique=True)  # max one
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)


class TicketStatusHistory(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ticket_status_history"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    from_status: Mapped[TicketStatus | None] = mapped_column(
        enum_col(TicketStatus, "from_status")
    )
    to_status: Mapped[TicketStatus] = mapped_column(enum_col(TicketStatus, "to_status"))
    changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
