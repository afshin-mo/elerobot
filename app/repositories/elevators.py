from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Building, Elevator


class ElevatorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, company_id: int, elevator_id: int) -> Elevator | None:
        stmt = select(Elevator).where(Elevator.company_id == company_id, Elevator.id == elevator_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_token(self, company_id: int, token: str) -> Elevator | None:
        stmt = select(Elevator).where(Elevator.company_id == company_id, Elevator.qr_token == token)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_name(self, company_id: int, building_id: int, name: str) -> Elevator | None:
        stmt = select(Elevator).where(
            Elevator.company_id == company_id, Elevator.building_id == building_id,
            Elevator.name == name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_building(self, company_id: int, building_id: int) -> list[Elevator]:
        stmt = (
            select(Elevator)
            .where(Elevator.company_id == company_id, Elevator.building_id == building_id)
            .order_by(Elevator.name, Elevator.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_active_for_owner(
        self, company_id: int, owner_id: int
    ) -> list[tuple[Elevator, Building]]:
        """Active elevators in active buildings of one customer (what can be reported)."""
        stmt = (
            select(Elevator, Building)
            .join(Building, Elevator.building_id == Building.id)
            .where(
                Building.company_id == company_id, Building.owner_id == owner_id,
                Building.is_active.is_(True), Elevator.is_active.is_(True),
            )
            .order_by(Building.name, Elevator.name)
        )
        return [(e, b) for e, b in (await self.session.execute(stmt)).all()]

    async def add(self, elevator: Elevator) -> Elevator:
        self.session.add(elevator)
        await self.session.flush()
        return elevator
