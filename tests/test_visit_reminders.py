"""Visit reminders: window (48 h to 2 h ahead), once per time, reachability, scheduler."""
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot

from app.bot.scheduler import run_reminders_once
from app.core.i18n import t
from app.core.outbox import Outbox
from app.core.timeutil import LOCAL_TZ
from app.models import Building, Company, Elevator, Role, ServiceVisit, User, VisitStatus
from app.services.visit_reminders import VisitReminderService
from app.services.visits import VisitService
from bot_helpers import FakeSession

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=LOCAL_TZ)  # 10:00 local, inside the send window


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    owner = User(company_id=company.id, phone="+9647500000003", name="Owner", role=Role.CUSTOMER,
                 telegram_id=103, language="fa")
    boss = User(company_id=company.id, phone="+9647500000001", name="Boss", role=Role.MANAGER,
                telegram_id=101, language="en")
    session.add_all([owner, boss])
    await session.flush()
    building = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    session.add(building)
    await session.flush()
    elevator = Elevator(company_id=company.id, building_id=building.id, name="Lift 1")
    session.add(elevator)
    await session.flush()

    async def visit(hours_ahead: float, status=VisitStatus.SCHEDULED) -> ServiceVisit:
        v = ServiceVisit(company_id=company.id, elevator_id=elevator.id, status=status,
                         scheduled_at=NOW + timedelta(hours=hours_ahead))
        session.add(v)
        await session.flush()
        return v

    return company, owner, boss, visit


async def _run(session, company, now=NOW):
    box = Outbox()
    queued = await VisitReminderService(session, company, box).run(now)
    return queued, box.items


async def test_reminder_content_and_buttons_data(session, world):
    company, owner, _, visit = world
    v = await visit(30)
    queued, items = await _run(session, company)
    assert queued == 1
    n = items[0]
    assert (n.telegram_id, n.language, n.key, n.action, n.visit_id) == (103, "fa", "notif_visit_reminder", "remind", v.id)
    assert n.params == {"id": v.id, "elevator": "Lift 1", "building": "Tower A", "when": (NOW + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")}
    assert v.reminder_sent_at is not None


async def test_window_edges(session, world):
    company, _, _, visit = world
    far = await visit(48.5)      # more than 48 h away: not yet
    inside = await visit(48)     # exactly 48 h: yes
    close = await visit(2)       # 2 h or less: too late, skipped
    past = await visit(-1)
    assert (await _run(session, company))[0] == 1
    assert inside.reminder_sent_at is not None
    assert all(v.reminder_sent_at is None for v in (far, close, past))
    # a day later the 48.5 h visit enters the window
    assert (await _run(session, company, NOW + timedelta(hours=1)))[0] == 1
    assert far.reminder_sent_at is not None


async def test_once_per_time_and_again_after_a_reschedule(session, world):
    company, owner, boss, visit = world
    v = await visit(30)
    assert (await _run(session, company))[0] == 1
    assert (await _run(session, company, NOW + timedelta(hours=1)))[0] == 0
    new_time = datetime.now(timezone.utc).astimezone(LOCAL_TZ).replace(second=0, microsecond=0) + timedelta(days=3)
    await VisitService(session, company, boss, Outbox()).reschedule(v.id, new_time.strftime("%Y-%m-%d %H:%M"))
    assert v.reminder_sent_at is None
    assert (await _run(session, company, new_time - timedelta(hours=30)))[0] == 1  # reminded again


async def test_only_visits_waiting_for_the_customer_are_reminded(session, world):
    company, _, _, visit = world
    for status in (VisitStatus.CONFIRMED, VisitStatus.CHANGE_REQUESTED, VisitStatus.CANCELLED, VisitStatus.DONE):
        await visit(30, status)
    assert (await _run(session, company))[0] == 0


async def test_unreachable_or_inactive_customer(session, world):
    company, owner, _, visit = world
    v = await visit(30)
    owner.telegram_id = None
    await session.flush()
    assert (await _run(session, company))[0] == 0 and v.reminder_sent_at is None
    owner.telegram_id = 103
    owner.is_active = False
    await session.flush()
    assert (await _run(session, company))[0] == 0
    owner.is_active = True
    await session.flush()
    assert (await _run(session, company))[0] == 1  # the reminder was not lost


async def test_scheduler_delivers_the_reminder_with_buttons_and_respects_the_window(session, world):
    company, owner, _, visit = world
    v = await visit(30)
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    night = NOW.replace(hour=22)
    assert await run_reminders_once(bot, company, now=night, session=session) == 0
    assert fake.sent == [] and v.reminder_sent_at is None
    assert await run_reminders_once(bot, company, now=NOW, session=session) == 1
    assert fake.last_to(103) == t("fa", "notif_visit_reminder", elevator="Lift 1", building="Tower A",
                                  when=(NOW + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M"))
