"""Manager menus: customers, technicians, buildings, elevators (stage 3).

Handlers only translate Telegram events into DirectoryService calls and render views.
Authorization is enforced by DirectoryService, not by which buttons are shown.
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.flow import register, start_flow
from app.core.i18n import t
from app.models import Company, Role, User
from app.services.auth import AuthService
from app.services.directory import DirectoryService
from app.services.errors import InvalidInput

log = logging.getLogger(__name__)

_USER_FIELDS = {"user_name", "phone"}
_BUILDING_FIELDS = {"building_name", "address", "notes"}
_ELEVATOR_FIELDS = {"elevator_name", "brand", "install_date", "notes"}
_ADD_FLOWS = {
    "add_customer": ("user_name", "phone"),
    "add_technician": ("user_name", "phone"),
    "add_manager": ("user_name", "phone"),
    "add_building": ("building_name", "address", "notes"),
    "add_elevator": ("elevator_name", "brand", "install_date", "notes"),
}


_ADD_ROLES = {
    "add_customer": Role.CUSTOMER,
    "add_technician": Role.TECHNICIAN,
    "add_manager": Role.MANAGER,
}


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    svc = DirectoryService(session, company, actor)  # raises PermissionDenied if not a manager
    return svc, (actor.language or company.default_language), actor


async def _show(event: Message | CallbackQuery, text: str, markup) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:  # "message is not modified"
            pass
    else:
        await event.answer(text, reply_markup=markup)


async def _users_list(svc, lang, role, page):
    users, total = await svc.list_users(role, page)
    return views.users_list(lang, role, users, total, page)


async def _user_card(svc, lang, user_id):
    user = await svc.get_user(user_id)
    buildings = await svc.list_buildings(user.id) if user.role is Role.CUSTOMER else []
    return views.user_card(lang, user, buildings, svc.actor.id)


async def _building_card(svc, lang, building_id):
    building = await svc.get_building(building_id)
    owner = await svc.get_user(building.owner_id)
    return views.building_card(lang, building, owner, await svc.list_elevators(building.id))


async def _elevator_card(svc, lang, elevator_id):
    elevator = await svc.get_elevator(elevator_id)
    return views.elevator_card(lang, elevator, await svc.get_building(elevator.building_id))


async def _finish(svc: DirectoryService, flow: str, parent, values: dict):
    """Apply a completed flow. Returns (prefix_key, text, markup)."""
    lang = svc.actor.language or svc.company.default_language
    if flow in _ADD_ROLES:
        role = _ADD_ROLES[flow]
        user = await svc.create_user(role, values["user_name"], values["phone"])
        return ("created", *await _user_card(svc, lang, user.id))
    if flow == "add_building":
        b = await svc.create_building(
            parent, values["building_name"], values["address"], values.get("notes")
        )
        return ("created", *await _building_card(svc, lang, b.id))
    if flow == "add_elevator":
        e = await svc.create_elevator(
            parent, values["elevator_name"], values.get("brand"),
            values.get("install_date"), values.get("notes"),
        )
        return ("created", *await _elevator_card(svc, lang, e.id))
    field, value = next(iter(values.items()))
    if flow == "edit_user":
        if field == "phone":
            await svc.change_phone(parent, value)
        else:
            await svc.rename_user(parent, value)
        return ("saved", *await _user_card(svc, lang, parent))
    if flow == "edit_building":
        await svc.update_building(parent, field, value)
        return ("saved", *await _building_card(svc, lang, parent))
    if flow == "edit_elevator":
        await svc.update_elevator(parent, field, value)
        return ("saved", *await _elevator_card(svc, lang, parent))
    raise InvalidInput()


async def _manager_finisher(flow, session, company, actor, parent, values):
    return await _finish(DirectoryService(session, company, actor), flow, parent, values)


register(
    ("add_customer", "add_technician", "add_manager", "add_building", "add_elevator",
     "edit_user", "edit_building", "edit_elevator"),
    _manager_finisher,
)


def build_router() -> Router:
    router = Router()

    @router.callback_query(F.data == "m:menu")
    async def menu(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        _, _, actor = await _context(session, company, cb.from_user.id)
        await cb.answer()
        await state.clear()
        text, markup = views.main_menu(actor)
        await _show(cb, text, markup)

    # ---- users ----

    @router.callback_query(F.data.startswith("u:list:"))
    async def user_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, role, page = cb.data.split(":")
        view = await _users_list(svc, lang, Role(role), int(page))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("u:view:"))
    async def user_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        view = await _user_card(svc, lang, int(cb.data.split(":")[2]))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("u:add:"))
    async def user_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        flow = f"add_{cb.data.split(':')[2]}"
        if flow not in _ADD_FLOWS or flow not in _ADD_ROLES:
            await cb.answer()
            return
        svc.ensure_can_create(_ADD_ROLES[flow])
        await cb.answer()
        await start_flow(cb, state, lang, flow, _ADD_FLOWS[flow])

    @router.callback_query(F.data.startswith("u:edit:"))
    async def user_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, user_id, field = cb.data.split(":")
        await svc.get_user(int(user_id))
        await cb.answer()
        if field in _USER_FIELDS:
            await start_flow(cb, state, lang, "edit_user", (field,), int(user_id))

    @router.callback_query(F.data.startswith("u:act:"))
    async def user_activate(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, user_id, flag = cb.data.split(":")
        await svc.set_user_active(int(user_id), flag == "1")
        view = await _user_card(svc, lang, int(user_id))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("u:unlink:"))
    async def user_unlink_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        user = await svc.get_user(int(cb.data.split(":")[2]))
        await cb.answer()
        await _show(cb, *views.confirm_unlink(lang, user))

    @router.callback_query(F.data.startswith("u:unlink_ok:"))
    async def user_unlink(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        user_id = int(cb.data.split(":")[2])
        await svc.unlink_telegram(user_id)
        view = await _user_card(svc, lang, user_id)
        await cb.answer(t(lang, "unlinked"))
        await _show(cb, *view)

    # ---- buildings ----

    @router.callback_query(F.data.startswith("b:view:"))
    async def building_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        view = await _building_card(svc, lang, int(cb.data.split(":")[2]))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("b:add:"))
    async def building_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        owner_id = int(cb.data.split(":")[2])
        await svc.get_user(owner_id)
        await cb.answer()
        await start_flow(cb, state, lang, "add_building", _ADD_FLOWS["add_building"], owner_id)

    @router.callback_query(F.data.startswith("b:edit:"))
    async def building_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, building_id, field = cb.data.split(":")
        await svc.get_building(int(building_id))
        await cb.answer()
        if field in _BUILDING_FIELDS:
            await start_flow(cb, state, lang, "edit_building", (field,), int(building_id))

    @router.callback_query(F.data.startswith("b:act:"))
    async def building_activate(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, building_id, flag = cb.data.split(":")
        await svc.set_building_active(int(building_id), flag == "1")
        view = await _building_card(svc, lang, int(building_id))
        await cb.answer()
        await _show(cb, *view)

    # ---- elevators ----

    @router.callback_query(F.data.startswith("e:view:"))
    async def elevator_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        view = await _elevator_card(svc, lang, int(cb.data.split(":")[2]))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("e:add:"))
    async def elevator_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        building_id = int(cb.data.split(":")[2])
        await svc.get_building(building_id)
        await cb.answer()
        await start_flow(cb, state, lang, "add_elevator", _ADD_FLOWS["add_elevator"], building_id)

    @router.callback_query(F.data.startswith("e:edit:"))
    async def elevator_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, elevator_id, field = cb.data.split(":")
        await svc.get_elevator(int(elevator_id))
        await cb.answer()
        if field in _ELEVATOR_FIELDS:
            await start_flow(cb, state, lang, "edit_elevator", (field,), int(elevator_id))

    @router.callback_query(F.data.startswith("e:act:"))
    async def elevator_activate(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang, _ = await _context(session, company, cb.from_user.id)
        _, _, elevator_id, flag = cb.data.split(":")
        await svc.set_elevator_active(int(elevator_id), flag == "1")
        view = await _elevator_card(svc, lang, int(elevator_id))
        await cb.answer()
        await _show(cb, *view)

    return router
