"""Contract screens end to end with a fake Telegram session."""
from datetime import timedelta

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.core.timeutil import local_today
from app.models import Building, Company, Contract, ContractStatus, Role, User
from bot_helpers import FakeSession, Inject, button_update, text_update

MANAGER, OWNER, STRANGER, TECH = 101, 103, 104, 105


@pytest.fixture
async def env(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg, **kw):
        return User(company_id=company.id, phone=phone, name=kw.pop("name", "U"), role=role,
                    telegram_id=tg, language="en", **kw)

    boss = user("+9647500000001", Role.MANAGER, MANAGER, name="Boss", is_admin=True)
    owner = user("+9647500000003", Role.CUSTOMER, OWNER, name="Owner")
    stranger = user("+9647500000004", Role.CUSTOMER, STRANGER, name="Stranger")
    tech = user("+9647500000005", Role.TECHNICIAN, TECH, name="Tech")
    session.add_all([boss, owner, stranger, tech])
    await session.flush()
    mine = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    theirs = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([mine, theirs])
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 1000))

    async def send(factory, *args, **kw):
        kw.setdefault("tg_id", MANAGER)
        await dp.feed_update(bot, factory(*args, next(counter), **kw))

    return company, session, fake, send, mine, theirs


async def _contracts(session):
    return list((await session.execute(select(Contract).order_by(Contract.id))).scalars())


def _iso(days: int) -> str:
    return (local_today() + timedelta(days=days)).isoformat()


async def test_manager_adds_a_contract_from_the_building_card(env):
    company, session, fake, send, mine, _ = env
    await send(button_update, f"b:view:{mine.id}")
    await send(button_update, f"c:bl:{mine.id}")
    assert t("en", "no_contracts") in fake.sent[-1]
    await send(button_update, f"c:add:{mine.id}")
    assert fake.sent[-1].startswith(t("en", "prompt_start_date"))
    await send(text_update, _iso(-10))
    assert fake.sent[-1].startswith(t("en", "prompt_end_date"))
    await send(text_update, _iso(20))
    assert fake.sent[-1].startswith(t("en", "prompt_notes"))
    await send(text_update, "yearly service")

    [c] = await _contracts(session)
    assert c.building_id == mine.id and c.notes == "yearly service" and c.status is ContractStatus.ACTIVE
    assert fake.sent[-1].startswith(t("en", "created"))
    assert "Tower A" in fake.sent[-1] and "Owner" in fake.sent[-1] and "yearly service" in fake.sent[-1]
    assert t("en", "contract_days_left", days=20) in fake.sent[-1]


async def test_bad_dates_keep_the_form_open_or_report_the_rule(env):
    _, session, fake, send, mine, _ = env
    await send(button_update, f"c:add:{mine.id}")
    await send(text_update, "yesterday")
    assert fake.sent[-1] == t("en", "err_invalid_date")  # still waiting for the start date
    await send(text_update, _iso(10))
    await send(text_update, _iso(5))  # ends before it starts
    await send(text_update, "-")
    assert fake.sent[-1] == t("en", "err_contract_dates")
    assert await _contracts(session) == []


async def test_manager_edits_end_date_and_cancels(env):
    company, session, fake, send, mine, _ = env
    c = Contract(company_id=company.id, building_id=mine.id, start_date=local_today() - timedelta(days=100),
                 end_date=local_today() + timedelta(days=5))
    session.add(c)
    await session.flush()

    await send(button_update, f"c:edit:{c.id}:end_date")
    await send(text_update, _iso(60))
    assert c.end_date == local_today() + timedelta(days=60) and fake.sent[-1].startswith(t("en", "saved"))
    await send(button_update, f"c:cancel:{c.id}")
    assert fake.sent[-1] == t("en", "confirm_cancel_contract", id=c.id)
    assert c.status is ContractStatus.ACTIVE  # asking changes nothing
    await send(button_update, f"c:cancel_ok:{c.id}")
    assert c.status is ContractStatus.CANCELLED and t("en", "cs_cancelled") in fake.sent[-1]


async def test_manager_lists(env):
    company, session, fake, send, mine, theirs = env
    for b, days in ((mine, 5), (theirs, 90)):
        session.add(Contract(company_id=company.id, building_id=b.id,
                             start_date=local_today() - timedelta(days=100),
                             end_date=local_today() + timedelta(days=days)))
    await session.flush()
    await send(button_update, "c:menu")
    await send(button_update, "c:list:expiring:0")
    assert fake.sent[-1] == t("en", "cg_expiring") + " (1)"
    await send(button_update, "c:list:active:0")
    assert fake.sent[-1] == t("en", "cg_active") + " (2)"
    await send(button_update, "c:list:ended:0")
    assert fake.sent[-1].startswith(t("en", "cg_ended") + " (0)")


async def test_customer_sees_own_contract_and_asks_for_renewal(env):
    company, session, fake, send, mine, theirs = env
    c = Contract(company_id=company.id, building_id=mine.id, start_date=local_today() - timedelta(days=100),
                 end_date=local_today() + timedelta(days=9))
    other = Contract(company_id=company.id, building_id=theirs.id, start_date=local_today() - timedelta(days=100),
                     end_date=local_today() + timedelta(days=9))
    session.add_all([c, other])
    await session.flush()

    await send(button_update, "c:list:mine:0", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "title_my_contracts") + " (1)"
    await send(button_update, f"c:view:{c.id}", tg_id=OWNER)
    assert "Tower A" in fake.sent[-1]
    await send(button_update, f"c:view:{other.id}", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "err_not_found")

    await send(button_update, f"c:renew:{c.id}", tg_id=OWNER)
    assert t("en", "msg_renewal_sent") in fake.sent  # the customer's answer ...
    assert fake.last_to(MANAGER) == t("en", "notif_renewal_requested", id=c.id, customer="Owner",
                                      building="Tower A", end=c.end_date.isoformat())
    await send(button_update, f"c:renew:{c.id}", tg_id=OWNER)  # too soon
    assert fake.sent[-1] == t("en", "err_renewal_recent")


async def test_customer_and_technician_cannot_manage_contracts(env):
    company, session, fake, send, mine, _ = env
    c = Contract(company_id=company.id, building_id=mine.id, start_date=local_today() - timedelta(days=100),
                 end_date=local_today() + timedelta(days=9))
    session.add(c)
    await session.flush()
    for tg in (OWNER, TECH):
        for data in ("c:menu", f"c:bl:{mine.id}", f"c:add:{mine.id}"):
            await send(button_update, data, tg_id=tg)
            assert fake.sent[-1] == t("en", "no_permission"), (tg, data)
    for data in (f"c:cancel:{c.id}", f"c:cancel_ok:{c.id}", f"c:edit:{c.id}:end_date"):
        await send(button_update, data, tg_id=OWNER)
        assert fake.sent[-1] == t("en", "no_permission"), data
    assert c.status is ContractStatus.ACTIVE
    await send(button_update, f"c:renew:{c.id}", tg_id=TECH)  # technicians have no contract access
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"c:renew:{c.id}", tg_id=MANAGER)  # only customers request renewals
    assert fake.sent[-1] == t("en", "no_permission")


async def test_garbled_callbacks_are_rejected_politely(env):
    _, _, fake, send, _, _ = env
    for data in ("c:view:abc", "c:list:active", "c:list:active:x", "c:bl:", "c:edit:1"):
        await send(button_update, data)
        assert fake.sent[-1] == t("en", "err_invalid_text"), data
