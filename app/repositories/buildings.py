from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Building


class BuildingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, company_id: int, building_id: int) -> Building | None:
        stmt = select(Building).where(Building.company_id == company_id, Building.id == building_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_owner(self, company_id: int, owner_id: int) -> list[Building]:
        stmt = (
            select(Building)
            .where(Building.company_id == company_id, Building.owner_id == owner_id)
            .order_by(Building.name, Building.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def add(self, building: Building) -> Building:
        self.session.add(building)
        await self.session.flush()
        return building
