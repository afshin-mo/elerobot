from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Building, Contract, ContractReminder, ContractStatus

EXPIRING_DAYS = 30  # the "expiring soon" window of the manager list


def _group_filter(group: str, today: date):
    """WHERE conditions of a manager list group."""
    if group == "expiring":
        return [
            Contract.status == ContractStatus.ACTIVE,
            Contract.end_date >= today, Contract.end_date <= today + timedelta(days=EXPIRING_DAYS),
        ]
    if group == "active":
        return [Contract.status == ContractStatus.ACTIVE, Contract.end_date >= today]
    if group == "ended":
        return [or_(Contract.status == ContractStatus.CANCELLED, Contract.end_date < today)]
    raise ValueError(group)


class ContractRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, contract: Contract) -> Contract:
        self.session.add(contract)
        await self.session.flush()
        return contract

    async def get(self, company_id: int, contract_id: int) -> Contract | None:
        stmt = select(Contract).where(Contract.company_id == company_id, Contract.id == contract_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_building(self, company_id: int, building_id: int) -> list[Contract]:
        stmt = (
            select(Contract)
            .where(Contract.company_id == company_id, Contract.building_id == building_id)
            .order_by(Contract.end_date.desc(), Contract.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def overlapping(
        self, company_id: int, building_id: int, start: date, end: date, exclude_id: int | None = None
    ) -> Contract | None:
        """An active contract of the building whose period touches [start, end]."""
        stmt = select(Contract).where(
            Contract.company_id == company_id, Contract.building_id == building_id,
            Contract.status == ContractStatus.ACTIVE,
            Contract.start_date <= end, Contract.end_date >= start,
        )
        if exclude_id is not None:
            stmt = stmt.where(Contract.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def list_group(
        self, company_id: int, group: str, today: date, offset: int, limit: int,
        owner_id: int | None = None,
    ) -> list[tuple[Contract, Building]]:
        """Manager groups: expiring / active / ended. With owner_id (a customer): all of
        that customer's contracts, newest end date first."""
        stmt = (
            select(Contract, Building)
            .join(Building, Contract.building_id == Building.id)
            .where(Contract.company_id == company_id)
        )
        if owner_id is not None:
            stmt = stmt.where(Building.owner_id == owner_id).order_by(
                Contract.end_date.desc(), Contract.id.desc()
            )
        else:
            order = Contract.end_date.desc() if group == "ended" else Contract.end_date.asc()
            stmt = stmt.where(*_group_filter(group, today)).order_by(order, Contract.id)
        rows = (await self.session.execute(stmt.offset(offset).limit(limit))).all()
        return [(c, b) for c, b in rows]

    async def count_group(
        self, company_id: int, group: str, today: date, owner_id: int | None = None
    ) -> int:
        stmt = (
            select(func.count()).select_from(Contract)
            .join(Building, Contract.building_id == Building.id)
            .where(Contract.company_id == company_id)
        )
        if owner_id is not None:
            stmt = stmt.where(Building.owner_id == owner_id)
        else:
            stmt = stmt.where(*_group_filter(group, today))
        return (await self.session.execute(stmt)).scalar_one()

    async def list_due(self, company_id: int, today: date) -> list[tuple[Contract, Building]]:
        """Active contracts ending within the reminder window (today included)."""
        stmt = (
            select(Contract, Building)
            .join(Building, Contract.building_id == Building.id)
            .where(Contract.company_id == company_id, *_group_filter("expiring", today))
            .order_by(Contract.end_date, Contract.id)
        )
        return [(c, b) for c, b in (await self.session.execute(stmt)).all()]

    # ---- reminders already sent ----

    async def has_reminder(self, contract_id: int, days_before: int) -> bool:
        stmt = select(ContractReminder.id).where(
            ContractReminder.contract_id == contract_id, ContractReminder.days_before == days_before
        )
        return (await self.session.execute(stmt)).first() is not None

    async def add_reminder(
        self, company_id: int, contract_id: int, days_before: int, sent_at: datetime
    ) -> None:
        self.session.add(ContractReminder(
            company_id=company_id, contract_id=contract_id, days_before=days_before, sent_at=sent_at,
        ))
        await self.session.flush()

    async def clear_reminders(self, contract_id: int) -> None:
        await self.session.execute(
            delete(ContractReminder).where(ContractReminder.contract_id == contract_id)
        )
