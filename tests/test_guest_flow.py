"""Guest mode end to end with a fake Telegram session: public menu, consultation form,
manager notification and handling."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.models import Company, Lead, LeadStatus, Role, User
from bot_helpers import (
    FakeSession, Inject, button_update, contact_update, lang_update, text_update,
)

GUEST = 777
MANAGER = 101


@pytest.fixture
async def env(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    session.add_all([
        User(company_id=company.id, phone="+9647500000001", name="Boss", role=Role.MANAGER,
             telegram_id=MANAGER, language="en", is_admin=True),
        User(company_id=company.id, phone="+9647500000009", name="Cust", role=Role.CUSTOMER,
             telegram_id=909, language="en"),
    ])
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 1000))

    async def send(factory, *args, **kw):
        kw.setdefault("tg_id", GUEST)
        await dp.feed_update(bot, factory(*args, next(counter), **kw))

    return company, session, fake, send


async def _leads(session):
    return list((await session.execute(select(Lead).order_by(Lead.id))).scalars())


async def _consult(send, text="I want a quote for a new building"):
    await send(button_update, "g:consult:en")
    await send(contact_update, "9647501234567")
    await send(text_update, text)


async def test_start_shows_language_then_guest_menu(env):
    _, _, fake, send = env
    await send(text_update, "/start")
    await send(lang_update, "ckb")
    assert fake.sent[-1] == t("ckb", "guest_menu", company="Test Co")


async def test_info_screens_work_without_signing_in(env):
    _, session, fake, send = env
    for name in ("about", "services", "prices", "contact"):
        await send(button_update, f"g:{name}:fa")
        # the fake session records edits too; the text is the guest_<name> screen
        assert fake.sent[-1] == t("fa", f"guest_{name}", company="Test Co")
    await send(button_update, "g:menu:fa")
    assert fake.sent[-1] == t("fa", "guest_menu", company="Test Co")
    assert await _leads(session) == []


async def test_unknown_language_in_callback_is_ignored(env):
    _, _, fake, send = env
    await send(button_update, "g:about:xx")
    assert fake.sent == []


async def test_consultation_request_reaches_the_manager(env):
    company, session, fake, send = env
    await send(button_update, "g:consult:en")
    assert fake.sent[-1] == t("en", "guest_consult_intro") + "\n" + t("en", "cancel_hint")
    await send(contact_update, "9647501234567")
    assert fake.sent[-1].startswith(t("en", "guest_consult_ask_message"))
    await send(text_update, "I want a quote for a new building")

    [lead] = await _leads(session)
    assert lead.telegram_id == GUEST and lead.phone == "+9647501234567"
    assert lead.message == "I want a quote for a new building" and lead.status is LeadStatus.NEW
    assert lead.name == "Ali T" and lead.company_id == company.id
    assert fake.last_to(GUEST).startswith(t("en", "lead_received"))
    assert fake.last_to(MANAGER) == t(
        "en", "notif_new_lead", id=lead.id, name="Ali T", phone="+9647501234567", username="-",
        message="I want a quote for a new building",
    )
    assert 909 not in [chat for chat, _ in fake.sent_to]  # customers are not told


async def test_someone_elses_contact_card_is_refused(env):
    _, session, fake, send = env
    await send(button_update, "g:consult:en")
    await send(contact_update, "9647501234567", user_id=1)
    assert fake.sent[-1] == t("en", "share_own_contact_only")
    await send(text_update, "typing a number does not count")
    assert fake.sent[-1] == t("en", "share_phone_use_button", button=t("en", "share_phone_button"))
    assert await _leads(session) == []


async def test_invalid_contact_and_empty_message(env):
    _, session, fake, send = env
    await send(button_update, "g:consult:en")
    await send(contact_update, "12")
    assert fake.sent[-1] == t("en", "invalid_phone")
    await send(contact_update, "9647501234567")
    await send(text_update, "   ")
    assert fake.sent[-1] == t("en", "err_invalid_text")  # still waiting for the message
    await send(text_update, "Now a real message")
    assert len(await _leads(session)) == 1


async def test_daily_limit_blocks_the_form_up_front(env):
    _, session, fake, send = env
    for _ in range(3):
        await _consult(send)
    assert len(await _leads(session)) == 3
    await send(button_update, "g:consult:en")
    assert fake.sent[-1] == t("en", "err_lead_limit", max=3)
    await send(contact_update, "9647501234567")  # no form is open any more: nothing is stored
    assert len(await _leads(session)) == 3


async def test_cancel_returns_guest_to_the_menu(env):
    _, session, fake, send = env
    await send(button_update, "g:consult:fa")
    await send(text_update, "/cancel")
    assert fake.sent[-2] == t("fa", "cancelled")
    assert fake.sent[-1] == t("fa", "guest_menu", company="Test Co")
    await send(text_update, "late message")  # the form is closed: behaves like /start
    assert await _leads(session) == []


async def test_guest_can_still_sign_in_from_the_menu(env):
    _, session, fake, send = env
    await send(button_update, "g:login:en")
    assert fake.sent[-1] == t("en", "welcome", company="Test Co") + "\n" + t("en", "share_phone_prompt")
    await send(contact_update, "9647509999999")  # not registered
    assert fake.sent[-1] == t("en", "access_denied")


async def test_signed_in_users_still_get_their_menu(env):
    _, _, fake, send = env
    await send(text_update, "/start", tg_id=909)
    assert fake.sent[-1] == t("en", "main_menu", role=t("en", "role_customer"))


async def test_manager_works_the_request_list(env):
    _, session, fake, send = env
    await _consult(send)
    [lead] = await _leads(session)

    await send(button_update, "l:list:0", tg_id=MANAGER)
    assert fake.sent[-1] == t("en", "title_leads", total=1, new=1)
    await send(button_update, f"l:view:{lead.id}", tg_id=MANAGER)
    assert "+9647501234567" in fake.sent[-1] and "I want a quote" in fake.sent[-1]
    await send(button_update, f"l:set:{lead.id}:contacted", tg_id=MANAGER)
    assert lead.status is LeadStatus.CONTACTED and lead.handled_by_id is not None
    assert t("en", "lead_handled_by", name="Boss", when="") .split("(")[0] in fake.sent[-1]
    await send(button_update, f"l:set:{lead.id}:new", tg_id=MANAGER)  # cannot go back
    assert fake.sent[-1] == t("en", "err_lead_state")
    await send(button_update, f"l:set:{lead.id}:bogus", tg_id=MANAGER)
    assert fake.sent[-1] == t("en", "err_invalid_text")


async def test_non_managers_cannot_open_the_requests(env):
    _, session, fake, send = env
    await _consult(send)
    [lead] = await _leads(session)
    for tg, lang in ((909, "en"), (GUEST, "ckb")):  # a customer; a guest gets the company default
        for data in ("l:list:0", f"l:view:{lead.id}", f"l:set:{lead.id}:closed"):
            await send(button_update, data, tg_id=tg)
            assert fake.sent[-1] == t(lang, "no_permission")
    assert lead.status is LeadStatus.NEW
