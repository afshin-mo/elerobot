from sqlalchemy import BigInteger, String, Text, UniqueConstraint, Boolean, Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin, enum_col
from app.models.enums import Role


class User(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("company_id", "phone", name="uq_users_company_phone"),
        UniqueConstraint("company_id", "telegram_id", name="uq_users_company_telegram_id"),
    )

    phone: Mapped[str] = mapped_column(String(20))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL until first login
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(enum_col(Role, "role"))
    language: Mapped[str | None] = mapped_column(String(5))  # NULL until user picks one
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # managers only: may manage managers


class Building(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "buildings"

    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # customer
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Elevator(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "elevators"
    __table_args__ = (UniqueConstraint("building_id", "name", name="uq_elevators_building_name"),)

    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    brand: Mapped[str | None] = mapped_column(String(100))
    install_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
