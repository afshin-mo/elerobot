from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import LeadStatus


class Lead(Base, IdMixin, TenantMixin, TimestampMixin):
    """A consultation request from someone who is not signed in (guest mode)."""

    __tablename__ = "leads"

    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)  # the sender
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))  # Telegram display name
    phone: Mapped[str] = mapped_column(String(20))  # shared with the contact button
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[LeadStatus] = mapped_column(
        enum_col(LeadStatus, "status"), default=LeadStatus.NEW, index=True
    )
    handled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
