"""Expiry reminders: steps 30/15/7, no duplicates, skipped steps, reachability, send window."""
from datetime import date, datetime, timedelta, timezone

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.scheduler import run_reminders_once
from app.core.i18n import t
from app.core.outbox import Outbox
from app.models import Building, Company, Contract, ContractReminder, ContractStatus, Role, User
from app.services.contract_reminders import ContractReminderService, step_for
from app.services.contracts import ContractService
from bot_helpers import FakeSession

TODAY = date(2026, 11, 14)


def test_step_for():
    assert [step_for(d) for d in (-1, 0, 3, 7, 8, 15, 16, 30, 31, 400)] == [
        None, 7, 7, 7, 15, 15, 30, 30, None, None]


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    owner = User(company_id=company.id, phone="+9647500000003", name="Owner", role=Role.CUSTOMER,
                 telegram_id=103, language="fa")
    session.add(owner)
    await session.flush()
    building = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    session.add(building)
    await session.flush()

    async def contract(days_left: int, building=building) -> Contract:
        c = Contract(company_id=company.id, building_id=building.id,
                     start_date=TODAY - timedelta(days=200), end_date=TODAY + timedelta(days=days_left))
        session.add(c)
        await session.flush()
        return c

    return company, owner, building, contract


async def _run(session, company, today=TODAY):
    box = Outbox()
    queued = await ContractReminderService(session, company, box).run(today)
    return queued, box.items


async def test_reminder_goes_to_the_owner_with_the_right_content(session, world):
    company, owner, building, contract = world
    c = await contract(20)
    queued, items = await _run(session, company)
    assert queued == 1
    n = items[0]
    assert (n.telegram_id, n.language, n.key, n.action) == (103, "fa", "notif_contract_expiring", "renew")
    assert n.contract_id == c.id
    assert n.params == {"id": c.id, "building": "Tower A", "end": (TODAY + timedelta(days=20)).isoformat(), "days": 20}


async def test_each_step_is_sent_once(session, world):
    company, _, _, contract = world
    c = await contract(30)
    assert (await _run(session, company))[0] == 1                 # 30 days left: first step
    assert (await _run(session, company))[0] == 0                 # same day again: nothing
    assert (await _run(session, company, TODAY + timedelta(days=10)))[0] == 0   # 20 left: still step 30
    assert (await _run(session, company, TODAY + timedelta(days=15)))[0] == 1   # 15 left
    assert (await _run(session, company, TODAY + timedelta(days=15)))[0] == 0
    assert (await _run(session, company, TODAY + timedelta(days=23)))[0] == 1   # 7 left
    assert (await _run(session, company, TODAY + timedelta(days=29)))[0] == 0   # 1 left: step 7 done
    steps = (await session.execute(select(ContractReminder.days_before).where(
        ContractReminder.contract_id == c.id).order_by(ContractReminder.days_before))).scalars().all()
    assert steps == [7, 15, 30]


async def test_missed_steps_are_skipped_not_sent_late(session, world):
    company, _, _, contract = world
    await contract(5)  # the bot was off: only the 7-day step applies, once
    assert (await _run(session, company))[0] == 1
    assert (await _run(session, company))[0] == 0


async def test_nothing_for_far_ended_or_cancelled_contracts(session, world):
    company, owner, building, contract = world
    await contract(31)        # too far away
    await contract(-1, building)  # already over
    cancelled = await contract(10)
    cancelled.status = ContractStatus.CANCELLED
    await session.flush()
    assert (await _run(session, company))[0] == 0


async def test_unreachable_customer_is_not_recorded_so_the_reminder_comes_later(session, world):
    company, owner, _, contract = world
    await contract(10)
    owner.telegram_id = None  # never signed in
    await session.flush()
    assert (await _run(session, company))[0] == 0
    assert (await session.execute(select(ContractReminder))).first() is None
    owner.telegram_id = 103   # signs in later
    await session.flush()
    assert (await _run(session, company))[0] == 1


async def test_inactive_customer_gets_nothing(session, world):
    company, owner, _, contract = world
    await contract(10)
    owner.is_active = False
    await session.flush()
    assert (await _run(session, company))[0] == 0


async def test_extended_contract_gets_reminders_again(session, world):
    company, owner, building, contract = world
    manager = User(company_id=company.id, phone="+9647500000001", name="Boss", role=Role.MANAGER, language="en")
    session.add(manager)
    await session.flush()
    c = await contract(10)
    assert (await _run(session, company))[0] == 1
    await ContractService(session, company, manager).update_field(
        c.id, "end_date", (TODAY + timedelta(days=12)).isoformat())
    assert (await _run(session, company))[0] == 1  # a new period: reminded again


async def _bot():
    fake = FakeSession()
    return Bot("123456:TEST", session=fake), fake


def _at(hour: int) -> datetime:
    """A UTC instant that is `hour`:00 in Iraq (UTC+3) on TODAY."""
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour, 0, tzinfo=timezone(timedelta(hours=3)))


async def test_scheduler_sends_inside_the_window_and_delivers_through_the_bot(session, world):
    company, owner, _, contract = world
    c = await contract(10)
    bot, fake = await _bot()
    sent = await run_reminders_once(bot, company, now=_at(10), session=session)
    assert sent == 1
    assert fake.last_to(103) == t("fa", "notif_contract_expiring", id=c.id, building="Tower A",
                                  end=(TODAY + timedelta(days=10)).isoformat(), days=10)
    assert await run_reminders_once(bot, company, now=_at(11), session=session) == 0  # already sent


@pytest.mark.parametrize("hour", [0, 7, 20, 23])
async def test_scheduler_stays_quiet_outside_08_to_20(session, world, hour):
    company, _, _, contract = world
    await contract(10)
    bot, fake = await _bot()
    assert await run_reminders_once(bot, company, now=_at(hour), session=session) == 0
    assert fake.sent == []
    assert (await session.execute(select(ContractReminder))).first() is None  # nothing recorded
