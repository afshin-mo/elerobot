"""Sign-in and language flows with a fake Telegram session (no network)."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.models import Company, Role, User
from bot_helpers import (
    TG_ID, FakeSession, Inject, button_update, contact_update, lang_update, text_update,
)


@pytest.fixture
async def env(session):
    company = Company(name="هیدروکلاس")
    session.add(company)
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    return company, bot, dp, fake


async def _tg_user_row(session):
    return (await session.execute(select(User).where(User.telegram_id == TG_ID))).scalar_one()


async def test_registered_customer_signs_in_and_language_is_saved(session, env):
    company, bot, dp, fake = env
    session.add(User(company_id=company.id, phone="+9647501234567", name="Ali", role=Role.CUSTOMER))
    await session.flush()

    await dp.feed_update(bot, text_update("/start", 1))
    assert fake.sent[-1].count("\n") == 2  # prompt shown in all three languages
    await dp.feed_update(bot, lang_update("fa", 2))
    assert fake.sent[-1] == t("fa", "guest_menu", company="هیدروکلاس")  # guests see the public menu first
    await dp.feed_update(bot, button_update("g:login:fa", 3))
    assert "هیدروکلاس" in fake.sent[-1]
    await dp.feed_update(bot, contact_update("9647501234567", 4))

    user = await _tg_user_row(session)
    assert user.language == "fa"
    assert fake.sent[-2] == t("fa", "login_success", name="Ali")
    assert fake.sent[-1] == t("fa", "main_menu", role=t("fa", "role_customer"))

    await dp.feed_update(bot, text_update("hello", 5))  # any text -> menu again
    assert fake.sent[-1] == t("fa", "main_menu", role=t("fa", "role_customer"))


async def test_unregistered_number_is_denied(session, env):
    _, bot, dp, fake = env
    await dp.feed_update(bot, text_update("/start", 1))
    await dp.feed_update(bot, lang_update("en", 2))
    await dp.feed_update(bot, button_update("g:login:en", 3))
    await dp.feed_update(bot, contact_update("9647509999999", 4))
    assert fake.sent[-1] == t("en", "access_denied")


async def test_someone_elses_contact_is_rejected(session, env):
    _, bot, dp, fake = env
    await dp.feed_update(bot, text_update("/start", 1))
    await dp.feed_update(bot, lang_update("ckb", 2))
    await dp.feed_update(bot, button_update("g:login:ckb", 3))
    await dp.feed_update(bot, contact_update("9647501234567", 4, user_id=999))
    assert fake.sent[-1] == t("ckb", "share_own_contact_only")


async def test_signed_in_user_can_change_language(session, env):
    company, bot, dp, fake = env
    session.add(User(company_id=company.id, phone="+9647501234567", name="Ali",
                     role=Role.TECHNICIAN, telegram_id=TG_ID, language="en"))
    await session.flush()
    await dp.feed_update(bot, text_update("/language", 1))
    await dp.feed_update(bot, lang_update("ckb", 2))
    assert (await _tg_user_row(session)).language == "ckb"
    assert fake.sent[-1] == t("ckb", "main_menu", role=t("ckb", "role_technician"))


async def test_first_manager_from_env_id_can_sign_in(session, env):
    company, bot, _, fake = env
    dp = build_dispatcher(company, TG_ID, session_middleware=Inject(session))
    await dp.feed_update(bot, text_update("/start", 1))
    await dp.feed_update(bot, lang_update("en", 2))
    await dp.feed_update(bot, button_update("g:login:en", 3))
    await dp.feed_update(bot, contact_update("9647501110000", 4))
    user = await _tg_user_row(session)
    assert user.role is Role.MANAGER and user.language == "en" and user.is_admin
    assert fake.sent[-1] == t("en", "main_menu", role="Manager")


async def test_typed_number_is_not_accepted_and_button_hint_is_shown(session, env):
    company, bot, dp, fake = env
    session.add(User(company_id=company.id, phone="+9647501234567", name="Ali", role=Role.CUSTOMER))
    await session.flush()
    await dp.feed_update(bot, text_update("/start", 1))
    await dp.feed_update(bot, lang_update("fa", 2))
    await dp.feed_update(bot, button_update("g:login:fa", 3))
    await dp.feed_update(bot, text_update("07501234567", 4))
    assert fake.sent[-1] == t("fa", "share_phone_use_button", button=t("fa", "share_phone_button"))
    row = (await session.execute(select(User).where(User.phone == "+9647501234567"))).scalar_one()
    assert row.telegram_id is None  # typing must never sign anyone in
