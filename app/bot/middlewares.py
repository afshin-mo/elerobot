from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.core.db import session_scope
from app.bot.notifier import deliver
from app.core.i18n import t
from app.core.outbox import Outbox, current_outbox
from app.services.auth import AuthService
from app.services.errors import ServiceError


class DbSessionMiddleware(BaseMiddleware):
    """One database session (one transaction) per Telegram update."""

    async def __call__(self, handler, event, data):
        async with session_scope() as session:
            data["session"] = session
            return await handler(event, data)


class OutboxMiddleware(BaseMiddleware):
    """Collects notifications during an update and sends them after the commit.

    Must be registered before the session middleware so it wraps the commit.
    """

    async def __call__(self, handler, event, data):
        outbox = Outbox()
        token = current_outbox.set(outbox)
        try:
            result = await handler(event, data)  # the session middleware commits inside
        finally:
            current_outbox.reset(token)
        await deliver(data["bot"], outbox.items)  # skipped if the update failed (exception above)
        return result


class ServiceErrorMiddleware(BaseMiddleware):
    """Turns business-rule errors into a translated message for the user."""

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except ServiceError as exc:
            box = current_outbox.get()
            if box is not None:
                box.clear()  # the action did not happen: tell nobody
            company = data["company"]
            user = await AuthService(data["session"], company, None).get_by_telegram_id(
                event.from_user.id
            )
            lang = user.language if user else None
            if lang is None and data.get("state") is not None:  # guest: language chosen in the bot
                lang = (await data["state"].get_data()).get("language")
            text = t(lang or company.default_language, exc.key, **exc.params)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(text, show_alert=True)
                except TelegramBadRequest:  # the query was already answered
                    await event.message.answer(text)
            else:
                await event.answer(text)
