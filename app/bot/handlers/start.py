import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.bot.keyboards import language_keyboard, phone_keyboard
from app.bot.views import main_menu
from app.core.i18n import t
from app.models import Company, User
from app.services.auth import AuthService, LoginStatus
from app.services.errors import InvalidInput

log = logging.getLogger(__name__)


class Login(StatesGroup):
    waiting_phone = State()


_STATUS_TEXT_KEY = {
    LoginStatus.NOT_REGISTERED: "access_denied",
    LoginStatus.INACTIVE: "account_inactive",
    LoginStatus.INVALID_PHONE: "invalid_phone",
    LoginStatus.PHONE_LINKED_TO_OTHER: "phone_linked_other",
}


def _auth(session: AsyncSession, company: Company, initial_manager_telegram_id: int | None):
    return AuthService(session, company, initial_manager_telegram_id)


async def _send_menu(message: Message, user: User) -> None:
    text, markup = main_menu(user)
    await message.answer(text, reply_markup=markup)


async def _ask_language(message: Message, company: Company) -> None:
    prompt = "\n".join(t(code, "choose_language") for code in company.active_languages)
    await message.answer(prompt, reply_markup=language_keyboard(company.active_languages))


def build_router() -> Router:
    """A fresh router per dispatcher (a router can only be attached once)."""
    router = Router()

    @router.message(CommandStart())
    async def cmd_start(
        message: Message, state: FSMContext, session: AsyncSession, company: Company,
        initial_manager_telegram_id: int | None,
    ):
        await state.clear()
        user = await _auth(session, company, initial_manager_telegram_id).get_by_telegram_id(
            message.from_user.id
        )
        if user is not None and not user.is_active:
            await message.answer(t(user.language, "account_inactive"))
        elif user is not None and user.language:
            await _send_menu(message, user)
        else:
            await _ask_language(message, company)


    @router.message(Command("language"))
    async def cmd_language(message: Message, company: Company):
        await _ask_language(message, company)


    @router.callback_query(F.data.startswith("lang:"))
    async def on_language(
        callback: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company,
        initial_manager_telegram_id: int | None,
    ):
        code = callback.data.split(":", 1)[1]
        await callback.answer()
        if code not in company.active_languages or callback.message is None:
            return
        auth = _auth(session, company, initial_manager_telegram_id)
        user = await auth.get_by_telegram_id(callback.from_user.id)
        if user is not None and not user.is_active:
            await callback.message.answer(t(code, "account_inactive"))
        elif user is not None:  # signed in: just change the language
            await auth.set_language(user, code)
            await callback.message.answer(t(code, "language_set"))
            await _send_menu(callback.message, user)
        else:  # not signed in yet: guest menu (company info, consultation, "sign in" button)
            await state.clear()
            text, markup = views.guest_menu(code, company.name)
            await callback.message.answer(text, reply_markup=markup)


    @router.message(Login.waiting_phone, F.contact)
    async def on_contact(
        message: Message, state: FSMContext, session: AsyncSession, company: Company,
        initial_manager_telegram_id: int | None,
    ):
        lang = (await state.get_data()).get("language") or company.default_language
        contact = message.contact
        if contact.user_id != message.from_user.id:  # someone else's contact card
            await message.answer(t(lang, "share_own_contact_only"), reply_markup=phone_keyboard(lang))
            return

        auth = _auth(session, company, initial_manager_telegram_id)
        result = await auth.login(message.from_user.id, contact.phone_number, message.from_user.full_name)
        if result.status is not LoginStatus.OK:
            await state.clear()
            await message.answer(
                t(lang, _STATUS_TEXT_KEY[result.status]), reply_markup=ReplyKeyboardRemove()
            )
            return

        user = result.user
        if not user.language:
            try:
                await auth.set_language(user, lang)
            except InvalidInput:
                log.warning("Language %s not active for company", lang)
        await state.clear()
        await message.answer(
            t(user.language, "login_success", name=user.name), reply_markup=ReplyKeyboardRemove()
        )
        await _send_menu(message, user)


    @router.message(Login.waiting_phone)
    async def waiting_phone_other(message: Message, state: FSMContext, company: Company):
        lang = (await state.get_data()).get("language") or company.default_language
        await message.answer(
            t(lang, "share_phone_use_button", button=t(lang, "share_phone_button")),
            reply_markup=phone_keyboard(lang),
        )


    @router.message()
    async def fallback(
        message: Message, state: FSMContext, session: AsyncSession, company: Company,
        initial_manager_telegram_id: int | None,
    ):
        """Any other message behaves like /start (menus for each role arrive in later stages)."""
        await cmd_start(message, state, session, company, initial_manager_telegram_id)

    return router
