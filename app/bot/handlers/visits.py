"""Service visit screens (FR-14): managers plan and close visits, customers confirm, cancel or
ask for another time. Authorization is in VisitService."""
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.flow import register, start_flow
from app.core.i18n import t
from app.core.timeutil import LOCAL_TZ
from app.models import Company, Role, VisitStatus
from app.services.auth import AuthService
from app.services.errors import InvalidInput
from app.services.visits import VisitService

_EDIT_FIELDS = {"visit_time", "visit_repeat"}


def _ints(data: str, count: int, start: int = 2) -> list[int]:
    try:
        return [int(x) for x in data.split(":")[start:start + count]]
    except ValueError:
        raise InvalidInput() from None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _card(svc: VisitService, lang: str, visit_id: int):
    return views.visit_card(lang, await svc.get_details(visit_id), svc.actor, _now())


async def _finish(flow, session, company, actor, parent, values):
    svc = VisitService(session, company, actor)
    lang = actor.language or company.default_language
    if flow == "add_visit":
        visit = await svc.create(parent, values["visit_time"], values.get("visit_repeat"))
        return ("created", *await _card(svc, lang, visit.id))
    if flow == "visit_change":
        await svc.request_change(parent, values.get("change_note"))
        return ("msg_change_sent", *await _card(svc, lang, parent))
    field, value = next(iter(values.items()))
    if field == "visit_time":
        await svc.reschedule(parent, value)
    else:
        await svc.set_repeat(parent, value)
    return ("saved", *await _card(svc, lang, parent))


register(("add_visit", "edit_visit", "visit_change"), _finish)


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    svc = VisitService(session, company, actor)  # raises PermissionDenied for other roles
    return svc, (actor.language or company.default_language)


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


def _toast(lang: str, key: str, nxt) -> str:
    """Answer text; mentions the visit created by the repeat rule."""
    text = t(lang, key)
    if nxt is not None:
        text += "\n" + t(lang, "msg_visit_next", when=nxt.scheduled_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"))
    return text


def build_router() -> Router:
    router = Router()

    @router.callback_query(F.data == "v:menu")
    async def menu(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        counts = await svc.counts()
        await cb.answer()
        await state.clear()
        await _show(cb, *views.visits_home(lang, counts))

    @router.callback_query(F.data.startswith("v:list:"))
    async def visit_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        parts = cb.data.split(":")
        if len(parts) != 4:
            raise InvalidInput()
        group, page = parts[2], _ints(cb.data, 1, 3)[0]
        rows, total = await svc.list_visits(group, page)
        await cb.answer()
        await _show(cb, *views.visits_list(lang, group, rows, total, max(page, 0), _now()))

    @router.callback_query(F.data.startswith("v:view:"))
    async def visit_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        view = await _card(svc, lang, _ints(cb.data, 1)[0])
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("v:el:"))
    async def elevator_visits(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        elevator, building, visits = await svc.list_for_elevator(_ints(cb.data, 1)[0])
        await cb.answer()
        await _show(cb, *views.elevator_visits(lang, elevator, building, visits, _now()))

    @router.callback_query(F.data.startswith("v:add:"))
    async def visit_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        elevator_id = _ints(cb.data, 1)[0]
        await svc.list_for_elevator(elevator_id)  # manager + existence check
        await cb.answer()
        await start_flow(cb, state, lang, "add_visit", ("visit_time", "visit_repeat"), elevator_id)

    @router.callback_query(F.data.startswith("v:edit:"))
    async def visit_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        field = cb.data.split(":")[3] if cb.data.count(":") == 3 else ""
        if field not in _EDIT_FIELDS:
            raise InvalidInput()
        svc.require_manager()
        await svc.get_details(visit_id)
        await cb.answer()
        await start_flow(cb, state, lang, "edit_visit", (field,), visit_id)

    @router.callback_query(F.data.startswith("v:done:"))
    async def done_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        svc.require_manager()
        await svc.get_details(visit_id)
        await cb.answer()
        await _show(cb, *views.confirm_visit_action(lang, "done", visit_id))

    @router.callback_query(F.data.startswith("v:done_ok:"))
    async def done(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        _, nxt = await svc.mark_done(visit_id)
        view = await _card(svc, lang, visit_id)
        await cb.answer(_toast(lang, "msg_visit_done", nxt), show_alert=nxt is not None)
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("v:cx:"))
    async def cancel_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        await svc.get_details(visit_id)  # ownership check for customers
        await cb.answer()
        await _show(cb, *views.confirm_visit_action(lang, "cx", visit_id))

    @router.callback_query(F.data.startswith("v:cx_ok:"))
    async def cancel(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        _, nxt = await svc.cancel(visit_id)
        view = await _card(svc, lang, visit_id)
        await cb.answer(_toast(lang, "msg_visit_cancelled", nxt), show_alert=nxt is not None)
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("v:ok:"))
    async def confirm(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        await svc.confirm(visit_id)
        view = await _card(svc, lang, visit_id)
        await cb.answer(t(lang, "msg_visit_confirmed"))
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("v:chg:"))
    async def change(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        visit_id = _ints(cb.data, 1)[0]
        details = await svc.get_details(visit_id)  # ownership check
        if svc.actor.role is not Role.CUSTOMER or details.visit.status not in (
            VisitStatus.SCHEDULED, VisitStatus.CONFIRMED
        ):
            raise InvalidInput("err_visit_state")
        await cb.answer()
        await start_flow(cb, state, lang, "visit_change", ("change_note",), visit_id)

    return router
