"""Guest mode (FR-10, FR-11): public company info and a consultation request form.

Nobody here is signed in. The phone number comes only from Telegram's contact button and must
belong to the sender. Business rules (validation, daily limit, notifying managers) live in
GuestLeadService.
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.handlers.start import Login
from app.bot.keyboards import phone_keyboard
from app.core.i18n import t
from app.models import Company
from app.services.errors import InvalidInput
from app.services.leads import GuestLeadService
from app.services.validation import validate_field

log = logging.getLogger(__name__)


class Consult(StatesGroup):
    waiting_contact = State()  # data: language
    waiting_message = State()  # data: language, phone


def _parse(callback: CallbackQuery, company: Company) -> tuple[str, str] | None:
    """('info', 'fa') from 'g:info:fa'; None if the language is not one of the company's."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[2] not in company.active_languages:
        return None
    return parts[1], parts[2]


async def _show(callback: CallbackQuery, text: str, markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


def build_router() -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("g:"))
    async def on_guest_button(
        callback: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company,
    ):
        parsed = _parse(callback, company)
        await callback.answer()
        if parsed is None or callback.message is None:
            return
        action, lang = parsed
        if action == "menu":
            await state.clear()
            text, markup = views.guest_menu(lang, company.name)
            await _show(callback, text, markup)
        elif action in views.GUEST_INFO:
            text, markup = views.guest_info(lang, action, company.name)
            await _show(callback, text, markup)
        elif action == "login":
            await state.clear()
            await state.update_data(language=lang)
            await state.set_state(Login.waiting_phone)
            await callback.message.answer(
                t(lang, "welcome", company=company.name) + "\n" + t(lang, "share_phone_prompt"),
                reply_markup=phone_keyboard(lang),
            )
        elif action == "consult":
            await state.clear()
            await state.update_data(language=lang)  # lets errors below use the chosen language
            # refuse early, before the person writes anything
            await GuestLeadService(session, company).check_allowed(callback.from_user.id)
            await state.set_state(Consult.waiting_contact)
            await callback.message.answer(
                t(lang, "guest_consult_intro") + "\n" + t(lang, "cancel_hint"),
                reply_markup=phone_keyboard(lang),
            )

    @router.message(Consult.waiting_contact, F.contact)
    async def on_contact(message: Message, state: FSMContext, company: Company):
        lang = (await state.get_data()).get("language") or company.default_language
        contact = message.contact
        if contact.user_id != message.from_user.id:  # someone else's contact card
            await message.answer(t(lang, "share_own_contact_only"), reply_markup=phone_keyboard(lang))
            return
        try:
            phone = validate_field("phone", contact.phone_number)
        except InvalidInput:
            await message.answer(t(lang, "invalid_phone"), reply_markup=phone_keyboard(lang))
            return
        await state.update_data(phone=phone)
        await state.set_state(Consult.waiting_message)
        await message.answer(
            t(lang, "guest_consult_ask_message") + "\n" + t(lang, "cancel_hint"),
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Consult.waiting_contact, ~F.text.startswith("/"))
    async def waiting_contact_other(message: Message, state: FSMContext, company: Company):
        lang = (await state.get_data()).get("language") or company.default_language
        await message.answer(
            t(lang, "share_phone_use_button", button=t(lang, "share_phone_button")),
            reply_markup=phone_keyboard(lang),
        )

    @router.message(Consult.waiting_message, F.text, ~F.text.startswith("/"))
    async def on_message_text(
        message: Message, state: FSMContext, session: AsyncSession, company: Company,
    ):
        data = await state.get_data()
        lang = data.get("language") or company.default_language
        validate_field("lead_message", message.text)  # early feedback; the service validates again
        await GuestLeadService(session, company).submit(
            message.from_user.id, message.from_user.username, message.from_user.full_name,
            data["phone"], message.text,
        )
        await state.clear()
        text, markup = views.guest_menu(lang, company.name)
        await message.answer(t(lang, "lead_received") + "\n\n" + text, reply_markup=markup)

    @router.message(Consult.waiting_message, ~F.text)
    async def waiting_message_other(message: Message, company: Company):
        await message.answer(t(company.default_language, "send_text"))

    return router
