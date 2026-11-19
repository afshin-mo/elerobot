"""Ticket screens: customers report breakdowns, managers assign/review/close,
technicians start work and report (stage 4).

Authorization lives in TicketService; these handlers only translate events and render views.
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.flow import register, start_flow
from app.core.i18n import t
from app.models import Company, Role, User
from app.services.auth import AuthService
from app.services.errors import InvalidInput, NotFound
from app.services.tickets import TicketService
from app.services.validation import validate_field, validate_rating

log = logging.getLogger(__name__)


class NewTicket(StatesGroup):
    description = State()  # data: elevator_id, customer_id
    photo = State()  # data adds: description
    confirm = State()  # data adds: photo_file_id (optional)


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    return TicketService(session, company, actor), actor, (actor.language or company.default_language)


async def _show(event: Message | CallbackQuery, text: str, markup) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:  # "message is not modified"
            pass
    else:
        await event.answer(text, reply_markup=markup)


async def _card(svc: TicketService, lang: str, ticket_id: int):
    return views.ticket_card(lang, await svc.get_details(ticket_id), svc.actor)


def _id(callback: CallbackQuery, index: int = 2) -> int:
    return int(callback.data.split(":")[index])


async def _ticket_finisher(flow, session, company, actor, parent, values):
    svc = TicketService(session, company, actor)
    lang = actor.language or company.default_language
    ticket_id = parent["ticket_id"] if isinstance(parent, dict) else parent
    if flow == "submit_report":
        await svc.submit_report(ticket_id, values["report"])
        prefix = "msg_report_sent"
    elif flow == "reject_report":
        await svc.reject_report(ticket_id, values["note"])
        prefix = "msg_report_rejected"
    else:  # submit_feedback
        await svc.submit_feedback(ticket_id, parent["rating"], values["comment"])
        prefix = "msg_feedback_sent"
    return (prefix, *await _card(svc, lang, ticket_id))


register(("submit_report", "reject_report", "submit_feedback"), _ticket_finisher)


def build_router() -> Router:
    router = Router()

    # ---- lists and cards ----

    @router.callback_query(F.data == "t:groups")
    async def groups(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        view = views.tickets_home(lang, await svc.counts())
        await cb.answer()
        await state.clear()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("t:list:"))
    async def ticket_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        _, _, group, page = cb.data.split(":")
        rows, total = await svc.list_tickets(group, int(page))
        view = views.ticket_list(lang, group, rows, total, int(page))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("t:view:"))
    async def ticket_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        view = await _card(svc, lang, _id(cb))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("t:photos:"))
    async def ticket_photos(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, _ = await _context(session, company, cb.from_user.id)
        details = await svc.get_details(_id(cb))
        await cb.answer()
        for file_id in details.photos:
            await cb.message.answer_photo(file_id, caption=f"#{details.ticket.id}")

    # ---- creating a ticket (customer, or manager on behalf of a customer) ----

    async def _pick_elevator(cb, state, session, company, customer_id, back):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        pairs = await svc.reportable_elevators(customer_id)
        await cb.answer()
        await state.clear()
        await state.update_data(customer_id=customer_id)
        if not pairs:
            await cb.message.answer(t(lang, "no_elevators_registered"))
            return
        await _show(cb, *views.elevator_picker(lang, pairs, back))

    @router.callback_query(F.data == "t:new")
    async def new_ticket(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _pick_elevator(cb, state, session, company, None, "m:menu")

    @router.callback_query(F.data.startswith("t:new_for:"))
    async def new_ticket_for(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        customer_id = _id(cb)
        await _pick_elevator(cb, state, session, company, customer_id, f"u:view:{customer_id}")

    @router.callback_query(F.data.startswith("t:pick:"))
    async def pick_elevator(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        customer_id = (await state.get_data()).get("customer_id")
        elevator_id = _id(cb)
        allowed = {e.id for e, _ in await svc.reportable_elevators(customer_id)}
        if elevator_id not in allowed:
            raise NotFound()
        await cb.answer()
        await state.set_state(NewTicket.description)
        await state.update_data(elevator_id=elevator_id, customer_id=customer_id)
        await cb.message.answer(t(lang, "prompt_description") + "\n" + t(lang, "cancel_hint"))

    @router.message(NewTicket.description, F.text, ~F.text.startswith("/"))
    async def got_description(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        _, _, lang = await _context(session, company, message.from_user.id)
        validate_field("description", message.text)
        await state.update_data(description=message.text.strip())
        await state.set_state(NewTicket.photo)
        await message.answer(t(lang, "prompt_photo") + "\n" + t(lang, "cancel_hint"))

    async def _summary(message: Message, state: FSMContext, session, company, photo_file_id):
        svc, _, lang = await _context(session, company, message.from_user.id)
        data = await state.get_data()
        elevator = await svc.elevators.get(company.id, data["elevator_id"])
        building = await svc.buildings.get(company.id, elevator.building_id)
        await state.update_data(photo_file_id=photo_file_id)
        await state.set_state(NewTicket.confirm)
        text, markup = views.ticket_summary(
            lang, elevator, building, data["description"], bool(photo_file_id)
        )
        await message.answer(text, reply_markup=markup)

    @router.message(NewTicket.photo, F.photo)
    async def got_photo(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        await _summary(message, state, session, company, message.photo[-1].file_id)

    @router.message(NewTicket.photo, F.text == "-")
    async def skip_photo(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        await _summary(message, state, session, company, None)

    @router.message(NewTicket.photo)
    async def photo_expected(message: Message, session: AsyncSession, company: Company):
        _, _, lang = await _context(session, company, message.from_user.id)
        await message.answer(t(lang, "prompt_photo") + "\n" + t(lang, "cancel_hint"))

    @router.callback_query(F.data == "t:confirm", NewTicket.confirm)
    async def confirm_ticket(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        data = await state.get_data()
        ticket = await svc.create_ticket(
            data["elevator_id"], data["description"], data.get("photo_file_id"), data.get("customer_id")
        )
        view = await _card(svc, lang, ticket.id)
        await cb.answer()
        await state.clear()
        await cb.message.answer(t(lang, "created") + "\n\n" + view[0], reply_markup=view[1])

    @router.callback_query(F.data == "t:confirm")
    async def confirm_without_draft(cb: CallbackQuery):
        raise InvalidInput("err_ticket_state")  # stale button: the draft is gone

    # ---- manager actions ----

    @router.callback_query(F.data.startswith("t:assign:"))
    async def assign_pick(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await svc.get_details(ticket_id)
        technicians = await svc.active_technicians()
        if not technicians:
            raise InvalidInput("err_technician_invalid")
        await cb.answer()
        await _show(cb, *views.technician_picker(lang, ticket_id, technicians))

    async def _act(cb, session, company, action, message_key):
        """Run a service action, then redraw the ticket card."""
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await action(svc, ticket_id)
        view = await _card(svc, lang, ticket_id)
        await cb.answer(t(lang, message_key))
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("t:set:"))
    async def assign_set(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id, technician_id = _id(cb), _id(cb, 3)
        await svc.assign(ticket_id, technician_id)
        details = await svc.get_details(ticket_id)
        reachable = bool(details.technician.telegram_id)
        view = views.ticket_card(lang, details, svc.actor)
        key = "msg_technician_assigned" if reachable else "msg_technician_not_linked"
        await cb.answer(t(lang, key), show_alert=not reachable)
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("t:askfb:"))
    async def ask_feedback(cb: CallbackQuery, session: AsyncSession, company: Company):
        await _act(cb, session, company, lambda svc, tid: svc.request_feedback(tid),
                   "msg_feedback_requested")

    # ---- customer feedback ----

    @router.callback_query(F.data.startswith("t:feedback:"))
    async def feedback_rating(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await svc.check_feedback_allowed(ticket_id)
        await cb.answer()
        await _show(cb, *views.rating_picker(lang, ticket_id))

    @router.callback_query(F.data.startswith("t:rate:"))
    async def feedback_comment(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id, rating = _id(cb), validate_rating(_id(cb, 3))
        await svc.check_feedback_allowed(ticket_id)
        await cb.answer()
        await start_flow(cb, state, lang, "submit_feedback", ("comment",),
                         {"ticket_id": ticket_id, "rating": rating})

    @router.callback_query(F.data.startswith("t:approve:"))
    async def approve(cb: CallbackQuery, session: AsyncSession, company: Company):
        await _act(cb, session, company, lambda svc, tid: svc.approve_report(tid), "msg_report_approved")

    @router.callback_query(F.data.startswith("t:close:"))
    async def close(cb: CallbackQuery, session: AsyncSession, company: Company):
        await _act(cb, session, company, lambda svc, tid: svc.close(tid), "msg_ticket_closed")

    @router.callback_query(F.data.startswith("t:reject:"))
    async def reject(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await svc.get_details(ticket_id)
        await cb.answer()
        await start_flow(cb, state, lang, "reject_report", ("note",), ticket_id)

    @router.callback_query(F.data.startswith("t:cancel:"))
    async def cancel_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await svc.get_details(ticket_id)
        await cb.answer()
        await _show(cb, *views.confirm_cancel_ticket(lang, ticket_id))

    @router.callback_query(F.data.startswith("t:cancel_ok:"))
    async def cancel_ticket(cb: CallbackQuery, session: AsyncSession, company: Company):
        await _act(cb, session, company, lambda svc, tid: svc.cancel(tid), "msg_ticket_cancelled")

    # ---- technician actions ----

    @router.callback_query(F.data.startswith("t:start:"))
    async def start_work(cb: CallbackQuery, session: AsyncSession, company: Company):
        await _act(cb, session, company, lambda svc, tid: svc.start_work(tid), "msg_work_started")

    @router.callback_query(F.data.startswith("t:report:"))
    async def report(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, _, lang = await _context(session, company, cb.from_user.id)
        ticket_id = _id(cb)
        await svc.get_details(ticket_id)
        await cb.answer()
        await start_flow(cb, state, lang, "submit_report", ("report",), ticket_id)

    return router
