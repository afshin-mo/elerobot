"""Manager screens for consultation requests (FR-11). Authorization is in LeadService."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.models import Company, LeadStatus
from app.services.auth import AuthService
from app.services.errors import InvalidInput
from app.services.leads import LeadService


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    svc = LeadService(session, company, actor)  # raises PermissionDenied if not a manager
    return svc, (actor.language or company.default_language)


async def _show(callback: CallbackQuery, text: str, markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


def build_router() -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("l:list:"))
    async def on_list(callback: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, callback.from_user.id)
        await callback.answer()
        try:
            page = int(callback.data.split(":")[2])
        except ValueError:
            raise InvalidInput() from None
        leads, total, new = await svc.list_leads(page)
        text, markup = views.leads_list(lang, leads, total, new, max(page, 0))
        await _show(callback, text, markup)

    @router.callback_query(F.data.startswith("l:view:"))
    async def on_view(callback: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, callback.from_user.id)
        await callback.answer()
        try:
            lead_id = int(callback.data.split(":")[2])
        except ValueError:
            raise InvalidInput() from None
        lead, handler = await svc.get(lead_id)
        text, markup = views.lead_card(lang, lead, handler)
        await _show(callback, text, markup)

    @router.callback_query(F.data.startswith("l:set:"))
    async def on_set(callback: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, callback.from_user.id)
        parts = callback.data.split(":")
        try:
            lead_id, status = int(parts[2]), LeadStatus(parts[3])
        except (ValueError, IndexError):
            raise InvalidInput() from None
        await svc.set_status(lead_id, status)
        await callback.answer()
        lead, handler = await svc.get(lead_id)
        text, markup = views.lead_card(lang, lead, handler)
        await _show(callback, text, markup)

    return router
