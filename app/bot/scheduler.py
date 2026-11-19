"""Background loop that sends contract expiry reminders (FR-13) and service visit reminders (FR-14).

A plain asyncio task inside the bot process (no extra dependency, decision D-016 revised):
every hour it asks the reminder services what is due and sends the queued messages after the
commit. Reminders go out only between 08:00 and 20:00 local time.
"""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.notifier import deliver
from app.core.db import session_scope
from app.core.outbox import Outbox
from app.core.timeutil import LOCAL_TZ
from app.models import Company
from app.services.contract_reminders import ContractReminderService
from app.services.visit_reminders import VisitReminderService

log = logging.getLogger(__name__)

SEND_HOURS = range(8, 20)  # local hours in which reminders may be sent
INTERVAL_SECONDS = 3600


async def _collect(session: AsyncSession, company: Company, outbox: Outbox, local_now: datetime) -> None:
    await ContractReminderService(session, company, outbox).run(local_now.date())
    await VisitReminderService(session, company, outbox).run(local_now)


async def run_reminders_once(
    bot: Bot, company: Company, now: datetime | None = None, session: AsyncSession | None = None,
) -> int:
    """One pass. Returns the number of messages sent. `session` is for tests (no commit)."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    if local_now.hour not in SEND_HOURS:
        return 0
    outbox = Outbox()
    if session is not None:
        await _collect(session, company, outbox, local_now)
    else:
        async with session_scope() as own:
            await _collect(own, company, outbox, local_now)
    await deliver(bot, outbox.items)  # after the commit; failures are logged, never raised
    return len(outbox.items)


async def reminder_loop(bot: Bot, company: Company, interval: int = INTERVAL_SECONDS) -> None:
    while True:
        try:
            sent = await run_reminders_once(bot, company)
            if sent:
                log.info("Contract reminders sent: %s", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reminder pass failed; will retry at the next interval")
        await asyncio.sleep(interval)
