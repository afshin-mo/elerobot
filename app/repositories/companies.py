from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company


class CompanyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_first(self) -> Company | None:
        stmt = select(Company).order_by(Company.id).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, company: Company) -> Company:
        self.session.add(company)
        await self.session.flush()
        return company
