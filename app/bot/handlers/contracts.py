"""Contract screens (FR-13): managers manage contracts of buildings, customers see their own
and can ask for a renewal. Authorization is in ContractService."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.flow import register, start_flow
from app.core.i18n import t
from app.core.timeutil import local_today
from app.models import Company
from app.services.auth import AuthService
from app.services.contracts import ContractService
from app.services.errors import InvalidInput

_ADD_FIELDS = ("start_date", "end_date", "contract_notes")
_EDIT_FIELDS = {"end_date", "contract_notes"}


def _ints(data: str, count: int, start: int = 2) -> list[int]:
    try:
        return [int(x) for x in data.split(":")[start:start + count]]
    except ValueError:
        raise InvalidInput() from None


async def _card(svc: ContractService, lang: str, contract_id: int):
    details = await svc.get_details(contract_id)
    return views.contract_card(lang, details, svc.actor, local_today())


async def _finish(flow, session, company, actor, parent, values):
    svc = ContractService(session, company, actor)
    lang = actor.language or company.default_language
    if flow == "add_contract":
        contract = await svc.create(
            parent, values["start_date"], values["end_date"], values.get("contract_notes")
        )
        return ("created", *await _card(svc, lang, contract.id))
    field, value = next(iter(values.items()))
    await svc.update_field(parent, field, value)
    return ("saved", *await _card(svc, lang, parent))


register(("add_contract", "edit_contract"), _finish)


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    svc = ContractService(session, company, actor)  # raises PermissionDenied for other roles
    return svc, (actor.language or company.default_language)


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


def build_router() -> Router:
    router = Router()

    @router.callback_query(F.data == "c:menu")
    async def menu(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        counts = await svc.counts()
        await cb.answer()
        await state.clear()
        await _show(cb, *views.contracts_home(lang, counts))

    @router.callback_query(F.data.startswith("c:list:"))
    async def contract_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        parts = cb.data.split(":")
        if len(parts) != 4:
            raise InvalidInput()
        group, page = parts[2], _ints(cb.data, 1, 3)[0]
        rows, total = await svc.list_contracts(group, page)
        await cb.answer()
        await _show(cb, *views.contracts_list(lang, group, rows, total, max(page, 0)))

    @router.callback_query(F.data.startswith("c:view:"))
    async def contract_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        view = await _card(svc, lang, _ints(cb.data, 1)[0])
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("c:bl:"))
    async def building_contracts(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        building, contracts = await svc.list_for_building(_ints(cb.data, 1)[0])
        await cb.answer()
        await _show(cb, *views.building_contracts(lang, building, contracts, local_today()))

    @router.callback_query(F.data.startswith("c:add:"))
    async def contract_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        building_id = _ints(cb.data, 1)[0]
        await svc.list_for_building(building_id)  # manager + existence check
        await cb.answer()
        await start_flow(cb, state, lang, "add_contract", _ADD_FIELDS, building_id)

    @router.callback_query(F.data.startswith("c:edit:"))
    async def contract_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        contract_id = _ints(cb.data, 1)[0]
        field = cb.data.split(":")[3] if cb.data.count(":") == 3 else ""
        if field not in _EDIT_FIELDS:
            raise InvalidInput()
        svc.require_manager()
        await svc.get_details(contract_id)
        await cb.answer()
        await start_flow(cb, state, lang, "edit_contract", (field,), contract_id)

    @router.callback_query(F.data.startswith("c:cancel:"))
    async def contract_cancel_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        contract_id = _ints(cb.data, 1)[0]
        svc.require_manager()
        await svc.get_details(contract_id)
        await cb.answer()
        await _show(cb, *views.confirm_cancel_contract(lang, contract_id))

    @router.callback_query(F.data.startswith("c:cancel_ok:"))
    async def contract_cancel(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        contract_id = _ints(cb.data, 1)[0]
        await svc.cancel(contract_id)
        view = await _card(svc, lang, contract_id)
        await cb.answer(t(lang, "msg_contract_cancelled"))
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("c:renew:"))
    async def contract_renew(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        contract_id = _ints(cb.data, 1)[0]
        await svc.request_renewal(contract_id)
        await cb.answer(t(lang, "msg_renewal_sent"), show_alert=True)

    return router
