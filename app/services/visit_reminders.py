"""Reminders for service visits (FR-14): sent to the building owner when 48 hours or less are
left (but more than 2 hours), once per scheduled time. Run by the bot's background loop."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox
from app.core.timeutil import LOCAL_TZ
from app.models import Company, User
from app.repositories.users import UserRepository
from app.repositories.visits import VisitRepository

log = logging.getLogger(__name__)

REMIND_WITHIN = timedelta(hours=48)
MIN_LEAD = timedelta(hours=2)  # too late to ask for a confirmation: skipped


class VisitReminderService:
    def __init__(self, session: AsyncSession, company: Company, outbox: Outbox):
        self.company = company
        self.outbox = outbox
        self.visits = VisitRepository(session)
        self.users = UserRepository(session)

    async def run(self, now: datetime | None = None) -> int:
        """Queue the due reminders. Returns how many were queued."""
        now = now or datetime.now(timezone.utc)
        queued = 0
        for visit, elevator, building in await self.visits.list_for_reminder(
            self.company.id, now, MIN_LEAD, REMIND_WITHIN
        ):
            owner = await self.users.get_by_id(self.company.id, building.owner_id)
            if not self._can_receive(owner):
                continue  # not recorded: the customer gets it once they have signed in
            self.outbox.add(Notification(
                telegram_id=owner.telegram_id,
                language=owner.language or self.company.default_language,
                key="notif_visit_reminder",
                params={
                    "id": visit.id, "elevator": elevator.name, "building": building.name,
                    "when": visit.scheduled_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
                },
                visit_id=visit.id, action="remind",
            ))
            visit.reminder_sent_at = now
            queued += 1
        return queued

    @staticmethod
    def _can_receive(user: User | None) -> bool:
        return user is not None and user.is_active and bool(user.telegram_id)
