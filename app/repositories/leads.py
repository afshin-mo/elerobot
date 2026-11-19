from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, LeadStatus


class LeadRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, lead: Lead) -> Lead:
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def get(self, company_id: int, lead_id: int) -> Lead | None:
        stmt = select(Lead).where(Lead.company_id == company_id, Lead.id == lead_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(self, company_id: int, offset: int, limit: int) -> list[Lead]:
        """Open requests first (new, then contacted), then closed; newest first inside each."""
        rank = case((Lead.status == LeadStatus.NEW, 0), (Lead.status == LeadStatus.CONTACTED, 1), else_=2)
        stmt = (
            select(Lead).where(Lead.company_id == company_id)
            .order_by(rank, Lead.id.desc()).offset(offset).limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count(self, company_id: int, status: LeadStatus | None = None) -> int:
        stmt = select(func.count()).select_from(Lead).where(Lead.company_id == company_id)
        if status is not None:
            stmt = stmt.where(Lead.status == status)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_since(self, company_id: int, telegram_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(Lead).where(
            Lead.company_id == company_id, Lead.telegram_id == telegram_id,
            Lead.created_at >= since,
        )
        return (await self.session.execute(stmt)).scalar_one()
