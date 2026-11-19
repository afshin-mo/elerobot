from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class Company(Base, IdMixin, TimestampMixin):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200))
    default_language: Mapped[str] = mapped_column(String(5), default="ckb")
    active_languages: Mapped[list] = mapped_column(JSON, default=lambda: ["ckb", "fa", "en"])
    contact_info: Mapped[dict] = mapped_column(JSON, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
