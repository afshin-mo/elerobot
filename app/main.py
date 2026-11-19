"""Entry point: checks config, database and bot token, then starts Long Polling."""
import asyncio
import contextlib
import logging

from aiogram import Bot

from app.bot.scheduler import reminder_loop
from app.bot.setup import build_dispatcher
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import setup_logging
from app.services.auth import AuthService
from app.services.company import ensure_company

log = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")

    async with session_scope() as session:
        company = await ensure_company(session, settings.company_name)
        auth = AuthService(session, company, settings.initial_manager_telegram_id)
        await auth.ensure_initial_admin()
    log.info("Database connection OK (company: %s)", company.name)

    dp = build_dispatcher(company, settings.initial_manager_telegram_id)
    bot = Bot(settings.bot_token)
    try:
        me = await bot.get_me()
        log.info("Bot token OK: @%s. Polling started.", me.username)
        reminders = asyncio.create_task(reminder_loop(bot, company))
        try:
            await dp.start_polling(bot)
        finally:
            reminders.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reminders
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
