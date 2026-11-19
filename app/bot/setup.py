import logging

from aiogram import BaseMiddleware, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from app.bot.handlers import build_routers
from app.bot.middlewares import DbSessionMiddleware, OutboxMiddleware, ServiceErrorMiddleware
from app.core.i18n import t
from app.models import Company

log = logging.getLogger(__name__)


async def on_error(event: ErrorEvent, company: Company) -> bool:
    log.exception("Unhandled error while processing update", exc_info=event.exception)
    message = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    if message is not None:
        try:
            await message.answer(t(company.default_language, "error_generic"))
        except Exception:  # never let error reporting crash the bot
            log.exception("Could not send error message")
    return True


def build_dispatcher(
    company: Company,
    initial_manager_telegram_id: int | None,
    session_middleware: BaseMiddleware | None = None,
) -> Dispatcher:
    dp = Dispatcher(
        storage=MemoryStorage(),
        company=company,
        initial_manager_telegram_id=initial_manager_telegram_id,
    )
    dp.update.outer_middleware(OutboxMiddleware())  # outermost: sends after the commit
    dp.update.outer_middleware(session_middleware or DbSessionMiddleware())
    dp.message.middleware(ServiceErrorMiddleware())
    dp.callback_query.middleware(ServiceErrorMiddleware())
    dp.errors.register(on_error)
    for router in build_routers():
        dp.include_router(router)
    return dp
