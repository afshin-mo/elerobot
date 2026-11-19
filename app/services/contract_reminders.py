"""Expiry reminders for contracts (FR-13): 30, 15 and 7 days before the end date.

Run by the bot's background loop; the caller decides when ("today") and sends the queued
notifications after the commit. No Telegram code here.
"""
import logging
from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox
from app.models import Company, User
from app.repositories.contracts import ContractRepository
from app.repositories.users import UserRepository

log = logging.getLogger(__name__)

STEPS = (7, 15, 30)  # days before the end date, smallest first


def step_for(days_left: int) -> int | None:
    """The reminder step that applies now: the smallest step not yet passed.

    With 20 days left the 30-day step applies; with 10 left the 15-day step; with 3 left the
    7-day step. Older steps that were missed (the bot was off) are skipped, not sent late."""
    for step in STEPS:
        if 0 <= days_left <= step:
            return step
    return None


class ContractReminderService:
    def __init__(self, session: AsyncSession, company: Company, outbox: Outbox):
        self.company = company
        self.outbox = outbox
        self.contracts = ContractRepository(session)
        self.users = UserRepository(session)

    async def run(self, today: date) -> int:
        """Queue the due reminders. Returns how many were queued."""
        queued = 0
        for contract, building in await self.contracts.list_due(self.company.id, today):
            days_left = (contract.end_date - today).days
            step = step_for(days_left)
            if step is None or await self.contracts.has_reminder(contract.id, step):
                continue
            owner = await self.users.get_by_id(self.company.id, building.owner_id)
            if not self._can_receive(owner):
                continue  # not recorded: the customer gets it once they have signed in
            self.outbox.add(Notification(
                telegram_id=owner.telegram_id,
                language=owner.language or self.company.default_language,
                key="notif_contract_expiring",
                params={
                    "id": contract.id, "building": building.name,
                    "end": contract.end_date.isoformat(), "days": days_left,
                },
                contract_id=contract.id, action="renew",
            ))
            await self.contracts.add_reminder(
                self.company.id, contract.id, step, datetime.now(timezone.utc)
            )
            queued += 1
        return queued

    @staticmethod
    def _can_receive(user: User | None) -> bool:
        return user is not None and user.is_active and bool(user.telegram_id)
