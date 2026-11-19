"""Generic step-by-step text input (FSM) shared by manager and ticket screens.

A screen starts a flow with `start_flow`; every answer is validated as it arrives and,
after the last field, the registered finisher applies the result through a service.
"""
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.core.i18n import t
from app.models import Company, User
from app.services.auth import AuthService
from app.services.errors import InvalidInput, PermissionDenied
from app.services.validation import FIELDS, validate_field

FinishResult = tuple[str, str, InlineKeyboardMarkup | None]  # (i18n prefix key, text, markup)
Finisher = Callable[[str, AsyncSession, Company, User, Any, dict], Awaitable[FinishResult]]
_FINISHERS: dict[str, Finisher] = {}


class Flow(StatesGroup):
    input = State()  # data: flow, fields, idx, values, parent


def register(flows: Iterable[str], finisher: Finisher) -> None:
    for name in flows:
        _FINISHERS[name] = finisher


async def ask(event: Message | CallbackQuery, lang: str, field: str) -> None:
    spec = FIELDS[field]
    text = t(lang, f"prompt_{spec.short}")
    if not spec.required:
        text += " " + t(lang, "optional_hint")
    message = event.message if isinstance(event, CallbackQuery) else event
    await message.answer(text + "\n" + t(lang, "cancel_hint"))


async def start_flow(event, state: FSMContext, lang: str, flow: str, fields, parent=None) -> None:
    if flow not in _FINISHERS:
        raise InvalidInput()
    await state.set_state(Flow.input)
    await state.update_data(flow=flow, fields=list(fields), idx=0, values={}, parent=parent)
    await ask(event, lang, fields[0])


def build_router() -> Router:
    router = Router()

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        actor = await AuthService(session, company, None).get_by_telegram_id(message.from_user.id)
        guest_lang = (await state.get_data()).get("language")  # set by the guest flows
        lang = (actor.language if actor else None) or guest_lang or company.default_language
        await state.clear()
        await message.answer(t(lang, "cancelled"), reply_markup=ReplyKeyboardRemove())
        if actor is not None and actor.is_active and actor.language:
            text, markup = views.main_menu(actor)
            await message.answer(text, reply_markup=markup)
        elif actor is None:  # a guest stops the consultation form or the sign-in
            text, markup = views.guest_menu(lang, company.name)
            await message.answer(text, reply_markup=markup)

    @router.message(Flow.input, F.text, ~F.text.startswith("/"))
    async def flow_input(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        actor = await AuthService(session, company, None).get_by_telegram_id(message.from_user.id)
        if actor is None or not actor.is_active:
            raise PermissionDenied()
        lang = actor.language or company.default_language
        data = await state.get_data()
        fields, idx, values = data["fields"], data["idx"], dict(data["values"])
        field = fields[idx]
        validate_field(field, message.text)  # early feedback; the service validates again
        values[field] = message.text
        if idx + 1 < len(fields):
            await state.update_data(idx=idx + 1, values=values)
            await ask(message, lang, fields[idx + 1])
            return
        finisher = _FINISHERS[data["flow"]]
        prefix, text, markup = await finisher(
            data["flow"], session, company, actor, data.get("parent"), values
        )
        await state.clear()
        await message.answer(t(lang, prefix) + "\n\n" + text, reply_markup=markup)

    @router.message(Flow.input, ~F.text)
    async def flow_non_text(message: Message, company: Company):
        await message.answer(t(company.default_language, "send_text"))

    return router
