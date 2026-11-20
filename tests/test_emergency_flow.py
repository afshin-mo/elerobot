"""QR emergency alert end to end with a fake Telegram session."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.models import AlertStatus, Building, Company, Elevator, EmergencyAlert, Role, Ticket, User
from app.services.emergency import payload_for
from bot_helpers import FakeSession, Inject, button_update, text_update

MANAGER, OWNER, TECH, GUEST = 101, 103, 105, 777


@pytest.fixture
async def env(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg, name):
        return User(company_id=company.id, phone=phone, name=name, role=role, telegram_id=tg, language="en")

    session.add_all([
        user("+9647500000001", Role.MANAGER, MANAGER, "Boss"),
        user("+9647500000003", Role.CUSTOMER, OWNER, "Owner"),
        user("+9647500000005", Role.TECHNICIAN, TECH, "Tech"),
    ])
    await session.flush()
    owner = (await session.execute(select(User).where(User.telegram_id == OWNER))).scalar_one()
    building = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="12 Main St")
    session.add(building)
    await session.flush()
    lift = Elevator(company_id=company.id, building_id=building.id, name="Lift 1")
    session.add(lift)
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 2000))

    async def send(factory, *args, **kw):
        kw.setdefault("tg_id", MANAGER)
        await dp.feed_update(bot, factory(*args, next(counter), **kw))

    return company, session, fake, send, lift, building, owner


async def _alerts(session):
    return list((await session.execute(select(EmergencyAlert).order_by(EmergencyAlert.id))).scalars())


async def _qr(send, lift):
    await send(button_update, f"q:qr:{lift.id}")
    return lift.qr_token


async def test_manager_gets_a_printable_qr_and_can_renew_it(env):
    company, session, fake, send, lift, building, _ = env
    assert lift.qr_token is None
    await send(button_update, f"e:view:{lift.id}")
    token = await _qr(send, lift)
    link = f"https://t.me/TestBot?start={payload_for(token)}"
    assert len(token) == 22 and fake.sent[-1] == t("en", "qr_caption", elevator="Lift 1", building="Tower A", link=link)
    assert await _qr(send, lift) == token  # the same code again

    await send(button_update, f"q:rg:{lift.id}")
    assert fake.sent[-1] == t("en", "confirm_qr_new") and lift.qr_token == token  # asking changes nothing
    await send(button_update, f"q:rg_ok:{lift.id}")
    assert lift.qr_token != token
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST)  # the old printed code
    assert fake.sent[-1] == t("ckb", "emergency_invalid") and await _alerts(session) == []


async def test_guest_scan_alerts_the_managers_without_sign_in(env):
    company, session, fake, send, lift, building, owner = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="fa", username="ali_u")

    [alert] = await _alerts(session)
    assert (alert.telegram_id, alert.telegram_username, alert.status) == (GUEST, "ali_u", AlertStatus.NEW)
    screen = fake.last_to(GUEST)
    assert t("fa", "emergency_title") in screen and "Tower A — Lift 1" in screen
    assert t("fa", "emergency_sent") in screen and t("fa", "emergency_safety") in screen and t("fa", "emergency_numbers") in screen
    alert_text = fake.last_to(MANAGER)
    assert alert_text == t("en", "notif_emergency", id=alert.id, building="Tower A", address="12 Main St",
                           elevator="Lift 1", name="Ali T", username="@ali_u", when=alert_text.split("Time: ")[1])
    assert "Owner" not in alert_text and "+964" not in alert_text


async def test_language_follows_telegram_settings_and_the_buttons_never_resend(env):
    company, session, fake, send, lift, building, _ = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="ku")
    assert t("ckb", "emergency_title") in fake.last_to(GUEST)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST + 1, language_code="de")
    assert t(company.default_language, "emergency_title") in fake.last_to(GUEST + 1)
    assert len(await _alerts(session)) == 1  # the second scan came within ten minutes
    assert t(company.default_language, "emergency_recent") in fake.last_to(GUEST + 1)

    await send(button_update, f"q:l:en:r:{token}", tg_id=GUEST + 1)  # language button of the "recent" screen
    assert t("en", "emergency_recent") in fake.sent[-1] and t("en", "emergency_safety") in fake.sent[-1]
    assert len(await _alerts(session)) == 1
    await send(button_update, f"q:l:xx:s:{token}", tg_id=GUEST)   # unknown language, bad state, bad token: ignored
    await send(button_update, f"q:l:en:z:{token}", tg_id=GUEST)
    await send(button_update, "q:l:en:s:short", tg_id=GUEST)
    assert len(await _alerts(session)) == 1


async def test_signed_in_people_use_their_own_language_and_unknown_codes_say_so(env):
    company, session, fake, send, lift, building, _ = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=OWNER, language_code="fa")  # a customer, language en
    assert t("en", "emergency_title") in fake.last_to(OWNER)
    assert len(await _alerts(session)) == 1
    await send(text_update, "/start unit_" + "a" * 22, tg_id=GUEST, language_code="en")
    assert fake.sent[-1] == t("en", "emergency_invalid")
    await send(text_update, "/start unit_x", tg_id=GUEST, language_code="en")  # not even a token: normal start
    assert fake.sent[-1] == "\n".join(t(code, "choose_language") for code in company.active_languages)
    assert len(await _alerts(session)) == 1


async def test_normal_start_is_unaffected(env):
    _, _, fake, send, *_ = env
    await send(text_update, "/start", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "main_menu", role=t("en", "role_customer"))
    await send(text_update, "/start promo", tg_id=OWNER)  # another deep link
    assert fake.sent[-1] == t("en", "main_menu", role=t("en", "role_customer"))


async def test_inactive_elevator_says_so_and_sends_nothing(env):
    company, session, fake, send, lift, building, _ = env
    token = await _qr(send, lift)
    lift.is_active = False
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="en")
    screen = fake.last_to(GUEST)
    assert t("en", "emergency_inactive") in screen and t("en", "emergency_safety") not in screen
    assert await _alerts(session) == []


async def test_manager_creates_a_ticket_after_confirmation(env):
    company, session, fake, send, lift, building, owner = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="en")
    [alert] = await _alerts(session)

    await send(button_update, "q:list:0")
    assert fake.sent[-1] == t("en", "title_alerts", total=1, new=1)
    await send(button_update, f"q:view:{alert.id}")
    assert "Tower A" in fake.sent[-1] and "Owner" in fake.sent[-1] and "+9647500000003" in fake.sent[-1]
    await send(button_update, f"q:tk:{alert.id}")
    assert fake.sent[-1] == t("en", "confirm_alert_ticket", id=alert.id) and alert.status is AlertStatus.NEW
    await send(button_update, f"q:tk_ok:{alert.id}")
    assert alert.status is AlertStatus.TICKET_CREATED
    ticket = (await session.execute(select(Ticket))).scalar_one()
    assert ticket.customer_id == owner.id and alert.ticket_id == ticket.id
    assert t("en", "msg_alert_ticket_created", ticket=ticket.id) in fake.sent
    assert any(t("en", "as_ticket_created") in m for m in fake.sent)  # the alert card shows the new status
    await send(button_update, f"q:tk_ok:{alert.id}")  # a second press
    assert fake.sent[-1] == t("en", "err_alert_state") and len((await session.execute(select(Ticket))).all()) == 1


async def test_manager_dismisses_a_false_alarm(env):
    company, session, fake, send, lift, building, _ = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="en")
    [alert] = await _alerts(session)
    await send(button_update, f"q:dm:{alert.id}")
    assert alert.status is AlertStatus.DISMISSED and t("en", "as_dismissed") in fake.sent[-1]
    await send(button_update, f"q:dm:{alert.id}")
    assert fake.sent[-1] == t("en", "err_alert_state")


async def test_only_managers_use_the_manager_screens(env):
    company, session, fake, send, lift, building, _ = env
    token = await _qr(send, lift)
    await send(text_update, f"/start {payload_for(token)}", tg_id=GUEST, language_code="en")
    [alert] = await _alerts(session)
    for tg in (OWNER, TECH, GUEST):
        for data in ("q:list:0", f"q:view:{alert.id}", f"q:tk:{alert.id}", f"q:tk_ok:{alert.id}", f"q:dm:{alert.id}",
                     f"q:qr:{lift.id}", f"q:rg:{lift.id}", f"q:rg_ok:{lift.id}"):
            await send(button_update, data, tg_id=tg)
            lang = company.default_language if tg == GUEST else "en"  # a guest has no language of their own
            assert fake.sent[-1] == t(lang, "no_permission"), (tg, data)
    assert alert.status is AlertStatus.NEW and lift.qr_token == token


async def test_garbled_callbacks_are_rejected_politely(env):
    _, _, fake, send, *_ = env
    for data in ("q:view:abc", "q:list:", "q:qr:x", "q:tk:", "q:dm:x"):
        await send(button_update, data)
        assert fake.sent[-1] == t("en", "err_invalid_text"), data
