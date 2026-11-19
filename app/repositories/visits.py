from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Building, Elevator, ServiceVisit, VisitStatus

OPEN = (VisitStatus.SCHEDULED, VisitStatus.CONFIRMED, VisitStatus.CHANGE_REQUESTED)


def _group_filter(group: str, now: datetime):
    """WHERE conditions of a manager list group."""
    if group == "attention":  # the customer asked for another time, or the time has passed
        return [or_(
            ServiceVisit.status == VisitStatus.CHANGE_REQUESTED,
            and_(ServiceVisit.status.in_((VisitStatus.SCHEDULED, VisitStatus.CONFIRMED)),
                 ServiceVisit.scheduled_at < now),
        )]
    if group == "upcoming":
        return [ServiceVisit.status.in_((VisitStatus.SCHEDULED, VisitStatus.CONFIRMED)),
                ServiceVisit.scheduled_at >= now]
    if group == "history":
        return [ServiceVisit.status.in_((VisitStatus.CANCELLED, VisitStatus.DONE))]
    raise ValueError(group)


class VisitRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, visit: ServiceVisit) -> ServiceVisit:
        self.session.add(visit)
        await self.session.flush()
        return visit

    async def get(self, company_id: int, visit_id: int) -> ServiceVisit | None:
        stmt = select(ServiceVisit).where(ServiceVisit.company_id == company_id, ServiceVisit.id == visit_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_elevator(self, company_id: int, elevator_id: int, limit: int = 10) -> list[ServiceVisit]:
        """Open visits first (soonest first), then the latest closed ones."""
        stmt = (
            select(ServiceVisit)
            .where(ServiceVisit.company_id == company_id, ServiceVisit.elevator_id == elevator_id)
            .order_by(ServiceVisit.status.in_(OPEN).desc(), ServiceVisit.scheduled_at.desc())
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        # open ones ascending by time, closed ones newest first
        return sorted(rows, key=lambda v: (v.status not in OPEN, v.scheduled_at if v.status in OPEN else -v.scheduled_at.timestamp()))

    async def has_open_at(self, company_id: int, elevator_id: int, when: datetime) -> bool:
        stmt = select(ServiceVisit.id).where(
            ServiceVisit.company_id == company_id, ServiceVisit.elevator_id == elevator_id,
            ServiceVisit.status.in_(OPEN), ServiceVisit.scheduled_at == when,
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    def _base(self, company_id: int, stmt, owner_id: int | None):
        stmt = (
            stmt.join(Elevator, ServiceVisit.elevator_id == Elevator.id)
            .join(Building, Elevator.building_id == Building.id)
            .where(ServiceVisit.company_id == company_id)
        )
        if owner_id is not None:
            stmt = stmt.where(Building.owner_id == owner_id)
        return stmt

    async def list_group(
        self, company_id: int, group: str, now: datetime, offset: int, limit: int,
        owner_id: int | None = None,
    ) -> list[tuple[ServiceVisit, Elevator, Building]]:
        """Manager groups: attention / upcoming / history. With owner_id (a customer): that
        customer's open visits, soonest first."""
        stmt = self._base(company_id, select(ServiceVisit, Elevator, Building), owner_id)
        if owner_id is not None:
            stmt = stmt.where(ServiceVisit.status.in_(OPEN)).order_by(ServiceVisit.scheduled_at, ServiceVisit.id)
        else:
            order = ServiceVisit.scheduled_at.desc() if group == "history" else ServiceVisit.scheduled_at.asc()
            stmt = stmt.where(*_group_filter(group, now)).order_by(order, ServiceVisit.id)
        rows = (await self.session.execute(stmt.offset(offset).limit(limit))).all()
        return [(v, e, b) for v, e, b in rows]

    async def count_group(
        self, company_id: int, group: str, now: datetime, owner_id: int | None = None
    ) -> int:
        stmt = self._base(company_id, select(func.count()).select_from(ServiceVisit), owner_id)
        if owner_id is not None:
            stmt = stmt.where(ServiceVisit.status.in_(OPEN))
        else:
            stmt = stmt.where(*_group_filter(group, now))
        return (await self.session.execute(stmt)).scalar_one()

    async def list_for_reminder(
        self, company_id: int, now: datetime, min_ahead: timedelta, max_ahead: timedelta
    ) -> list[tuple[ServiceVisit, Elevator, Building]]:
        """Visits still waiting for the customer that start within the reminder window."""
        stmt = self._base(company_id, select(ServiceVisit, Elevator, Building), None).where(
            ServiceVisit.status == VisitStatus.SCHEDULED,
            ServiceVisit.reminder_sent_at.is_(None),
            ServiceVisit.scheduled_at > now + min_ahead,
            ServiceVisit.scheduled_at <= now + max_ahead,
        ).order_by(ServiceVisit.scheduled_at, ServiceVisit.id)
        return [(v, e, b) for v, e, b in (await self.session.execute(stmt)).all()]
