from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_telegram_id(self, company_id: int, telegram_id: int) -> User | None:
        stmt = select(User).where(User.company_id == company_id, User.telegram_id == telegram_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_phone(self, company_id: int, phone: str) -> User | None:
        stmt = select(User).where(User.company_id == company_id, User.phone == phone)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, company_id: int, user_id: int) -> User | None:
        stmt = select(User).where(User.company_id == company_id, User.id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_role(self, company_id: int, role: Role, offset: int, limit: int) -> list[User]:
        stmt = (
            select(User)
            .where(User.company_id == company_id, User.role == role)
            .order_by(User.name, User.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_active_by_role(self, company_id: int, role: Role, limit: int = 50) -> list[User]:
        stmt = (
            select(User)
            .where(User.company_id == company_id, User.role == role, User.is_active.is_(True))
            .order_by(User.name, User.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_by_role(self, company_id: int, role: Role) -> int:
        stmt = select(func.count()).select_from(User).where(
            User.company_id == company_id, User.role == role
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_managers(self, company_id: int) -> int:
        stmt = select(func.count()).select_from(User).where(
            User.company_id == company_id, User.role == Role.MANAGER
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user
