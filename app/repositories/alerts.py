from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertStatus, Building, Elevator, EmergencyAlert


class AlertRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, alert: EmergencyAlert) -> EmergencyAlert:
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def get(self, company_id: int, alert_id: int, for_update: bool = False) -> EmergencyAlert | None:
        stmt = select(EmergencyAlert).where(
            EmergencyAlert.company_id == company_id, EmergencyAlert.id == alert_id
        )
        if for_update:  # two managers pressing "create ticket" at once must not make two tickets
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self, company_id: int, offset: int, limit: int
    ) -> list[tuple[EmergencyAlert, Elevator, Building]]:
        """New alerts first, then the others; newest first inside each."""
        rank = case((EmergencyAlert.status == AlertStatus.NEW, 0), else_=1)
        stmt = (
            select(EmergencyAlert, Elevator, Building)
            .join(Elevator, EmergencyAlert.elevator_id == Elevator.id)
            .join(Building, Elevator.building_id == Building.id)
            .where(EmergencyAlert.company_id == company_id)
            .order_by(rank, EmergencyAlert.id.desc()).offset(offset).limit(limit)
        )
        return [(a, e, b) for a, e, b in (await self.session.execute(stmt)).all()]

    async def count(self, company_id: int, status: AlertStatus | None = None) -> int:
        stmt = select(func.count()).select_from(EmergencyAlert).where(EmergencyAlert.company_id == company_id)
        if status is not None:
            stmt = stmt.where(EmergencyAlert.status == status)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_elevator_since(self, company_id: int, elevator_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(EmergencyAlert).where(
            EmergencyAlert.company_id == company_id, EmergencyAlert.elevator_id == elevator_id,
            EmergencyAlert.created_at >= since,
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_for_account_since(self, company_id: int, telegram_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(EmergencyAlert).where(
            EmergencyAlert.company_id == company_id, EmergencyAlert.telegram_id == telegram_id,
            EmergencyAlert.created_at >= since,
        )
        return (await self.session.execute(stmt)).scalar_one()
