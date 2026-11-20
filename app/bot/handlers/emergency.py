"""QR emergency alert (FR-12): the scan works without sign-in; managers handle the alerts and
create the QR codes. Business rules are in app.services.emergency."""
import re

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.core.i18n import t
from app.core.qr import qr_png
from app.models import Company
from app.services.auth import AuthService
from app.services.emergency import (
    AlertService, EmergencyAccessService, QrCodeService, payload_for, token_from_payload,
)
from app.services.errors import InvalidInput

_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,32}")


def guess_language(code: str | None, company: Company) -> str:
    """Language for a person nobody knows: from Telegram's language setting."""
    code = (code or "").lower()
    for prefix, language in (("fa", "fa"), ("ku", "ckb"), ("ckb", "ckb"), ("en", "en")):
        if code.startswith(prefix) and language in company.active_languages:
            return language
    return company.default_language


def _int(data: str, index: int = 2) -> int:
    try:
        return int(data.split(":")[index])
    except (ValueError, IndexError):
        raise InvalidInput() from None


async def _manager(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    return actor, (actor.language if actor and actor.language else company.default_language)


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


async def _send_qr(bot: Bot, cb: CallbackQuery, lang: str, elevator, building, token: str) -> None:
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={payload_for(token)}"
    await cb.message.answer_photo(
        BufferedInputFile(qr_png(link), filename=f"elevator-{elevator.id}.png"),
        caption=t(lang, "qr_caption", elevator=elevator.name, building=building.name, link=link),
        reply_markup=views.qr_buttons(lang, elevator.id),
    )


def build_router() -> Router:
    router = Router()

    # ---------- the scan (anyone, no sign-in) ----------

    @router.message(CommandStart(deep_link=True))
    async def scan(
        message: Message, command: CommandObject, state: FSMContext, session: AsyncSession, company: Company,
    ):
        token = token_from_payload(command.args)
        if token is None:
            raise SkipHandler  # some other /start link: the normal start handles it
        user = await AuthService(session, company, None).get_by_telegram_id(message.from_user.id)
        lang = (user.language if user and user.language else None) or guess_language(
            message.from_user.language_code, company
        )
        await state.clear()
        outcome = await EmergencyAccessService(session, company).trigger(
            token, message.from_user.id, message.from_user.username, message.from_user.full_name
        )
        if outcome is None:
            await message.answer(t(lang, "emergency_invalid"))
            return
        text, markup = views.emergency_screen(lang, outcome.building, outcome.elevator, outcome.state, token)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("q:l:"))
    async def change_language(cb: CallbackQuery, session: AsyncSession, company: Company):
        parts = (cb.data or "").split(":")
        if len(parts) != 5 or parts[2] not in company.active_languages or not _TOKEN.fullmatch(parts[4]):
            await cb.answer()
            return
        state = views.emergency_state_from_code(parts[3])
        found = await EmergencyAccessService(session, company).lookup(parts[4])  # read only: no alert
        await cb.answer()
        if state is None or found is None:
            return
        elevator, building = found
        await _show(cb, *views.emergency_screen(parts[2], building, elevator, state, parts[4]))

    # ---------- managers: alerts ----------

    @router.callback_query(F.data.startswith("q:list:"))
    async def alert_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        page = _int(cb.data)
        rows, total, new = await AlertService(session, company, actor).list_alerts(page)
        await cb.answer()
        await _show(cb, *views.alerts_list(lang, rows, total, new, max(page, 0)))

    @router.callback_query(F.data.startswith("q:view:"))
    async def alert_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        details = await AlertService(session, company, actor).get_details(_int(cb.data))
        await cb.answer()
        await _show(cb, *views.alert_card(lang, details))

    @router.callback_query(F.data.startswith("q:tk:"))
    async def ticket_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        alert_id = _int(cb.data)
        await AlertService(session, company, actor).get_details(alert_id)
        await cb.answer()
        await _show(cb, *views.confirm_alert_ticket(lang, alert_id))

    @router.callback_query(F.data.startswith("q:tk_ok:"))
    async def ticket_create(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        svc = AlertService(session, company, actor)
        alert_id = _int(cb.data)
        ticket = await svc.create_ticket(alert_id)
        details = await svc.get_details(alert_id)
        await cb.answer(t(lang, "msg_alert_ticket_created", ticket=ticket.id), show_alert=True)
        await _show(cb, *views.alert_card(lang, details))

    @router.callback_query(F.data.startswith("q:dm:"))
    async def dismiss(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        svc = AlertService(session, company, actor)
        alert_id = _int(cb.data)
        await svc.dismiss(alert_id)
        details = await svc.get_details(alert_id)
        await cb.answer()
        await _show(cb, *views.alert_card(lang, details))

    # ---------- managers: QR codes ----------

    @router.callback_query(F.data.startswith("q:qr:"))
    async def qr_show(cb: CallbackQuery, bot: Bot, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        elevator, building, token = await QrCodeService(session, company, actor).get_or_create(_int(cb.data))
        await cb.answer()
        await _send_qr(bot, cb, lang, elevator, building, token)

    @router.callback_query(F.data.startswith("q:rg:"))
    async def qr_new_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        elevator_id = _int(cb.data)
        await QrCodeService(session, company, actor).get_or_create(elevator_id)  # manager + existence check
        await cb.answer()
        text, markup = views.confirm_qr_new(lang, elevator_id)
        await cb.message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("q:rg_ok:"))
    async def qr_new(cb: CallbackQuery, bot: Bot, session: AsyncSession, company: Company):
        actor, lang = await _manager(session, company, cb.from_user.id)
        elevator, building, token = await QrCodeService(session, company, actor).regenerate(_int(cb.data))
        await cb.answer(t(lang, "msg_qr_new"))
        await _send_qr(bot, cb, lang, elevator, building, token)

    return router
