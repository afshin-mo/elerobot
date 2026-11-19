from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company
from app.repositories.companies import CompanyRepository


async def ensure_company(session: AsyncSession, name: str) -> Company:
    """Single-company deployment: return the one company, creating it on first run."""
    repo = CompanyRepository(session)
    company = await repo.get_first()
    if company is None:
        company = await repo.add(Company(name=name))
    return company
