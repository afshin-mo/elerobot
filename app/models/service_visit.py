from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import RepeatUnit, VisitStatus


class ServiceVisit(Base, IdMixin, TenantMixin, TimestampMixin):
    """A planned periodic service of one elevator. Dates are entered by a manager; an optional
    repeat rule creates the next visit when this one is done or cancelled."""

    __tablename__ = "service_visits"
    __table_args__ = (
        CheckConstraint(
            "(repeat_every IS NULL) = (repeat_unit IS NULL) AND (repeat_every IS NULL OR repeat_every > 0)",
            name="repeat_rule",
        ),
    )

    elevator_id: Mapped[int] = mapped_column(ForeignKey("elevators.id"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[VisitStatus] = mapped_column(
        enum_col(VisitStatus, "status"), default=VisitStatus.SCHEDULED, index=True
    )
    repeat_every: Mapped[int | None] = mapped_column(SmallInteger)
    repeat_unit: Mapped[RepeatUnit | None] = mapped_column(enum_col(RepeatUnit, "repeat_unit"))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_note: Mapped[str | None] = mapped_column(Text)  # the time the customer prefers
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
