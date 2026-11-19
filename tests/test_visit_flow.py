"""Service visit screens end to end with a fake Telegram session."""
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.core.timeutil import LOCAL_TZ
from app.models import Building, Company, Elevator, RepeatUnit, Role, ServiceVisit, User, VisitStatus
from bot_helpers import FakeSession, Inject, button_update, text_update

MANAGER, OWNER, STRANGER, TECH = 101, 103, 104, 105


def _at(days: int, hour: int = 9) -> datetime:
    base = datetime.now(timezone.utc).astimezone(LOCAL_TZ) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


def _raw(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


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
        user("+9647500000004", Role.CUSTOMER, STRANGER, "Stranger"),
        user("+9647500000005", Role.TECHNICIAN, TECH, "Tech"),
    ])
    await session.flush()
    owner = (await session.execute(select(User).where(User.telegram_id == OWNER))).scalar_one()
    stranger = (await session.execute(select(User).where(User.telegram_id == STRANGER))).scalar_one()
    mine = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    theirs = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([mine, theirs])
    await session.flush()
    lift = Elevator(company_id=company.id, building_id=mine.id, name="Lift 1")
    other_lift = Elevator(company_id=company.id, building_id=theirs.id, name="Lift 2")
    session.add_all([lift, other_lift])
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 1000))

    async def send(factory, *args, **kw):
        kw.setdefault("tg_id", MANAGER)
        await dp.feed_update(bot, factory(*args, next(counter), **kw))

    async def make_visit(elevator, when, status=VisitStatus.SCHEDULED, **kw):
        v = ServiceVisit(company_id=company.id, elevator_id=elevator.id, scheduled_at=when, status=status, **kw)
        session.add(v)
        await session.flush()
        return v

    return session, fake, send, lift, other_lift, make_visit


async def _visits(session):
    return list((await session.execute(select(ServiceVisit).order_by(ServiceVisit.id))).scalars())


async def test_manager_adds_a_visit_from_the_elevator_card(env):
    session, fake, send, lift, _, _ = env
    await send(button_update, f"e:view:{lift.id}")
    await send(button_update, f"v:el:{lift.id}")
    assert t("en", "no_visits") in fake.sent[-1]
    await send(button_update, f"v:add:{lift.id}")
    assert fake.sent[-1].startswith(t("en", "prompt_visit_time"))
    await send(text_update, "tomorrow")
    assert fake.sent[-1] == t("en", "err_invalid_datetime")
    when = _at(5)
    await send(text_update, _raw(when))
    assert fake.sent[-1].startswith(t("en", "prompt_visit_repeat"))
    await send(text_update, "3m")

    [v] = await _visits(session)
    assert v.scheduled_at == when and (v.repeat_every, v.repeat_unit) == (3, RepeatUnit.MONTHS)
    card = next(m for m in fake.sent if m.startswith(t("en", "created")))  # the reply to the manager
    assert "Lift 1" in card and _raw(when) in card and "Owner" in card
    assert fake.last_to(OWNER) == t("en", "notif_visit_scheduled", elevator="Lift 1", building="Tower A", when=_raw(when))


async def test_past_time_and_bad_repeat_are_refused(env):
    session, fake, send, lift, _, _ = env
    await send(button_update, f"v:add:{lift.id}")
    await send(text_update, _raw(_at(-1)))
    await send(text_update, "-")
    assert fake.sent[-1] == t("en", "err_visit_past")
    await send(button_update, f"v:add:{lift.id}")
    await send(text_update, _raw(_at(2)))
    await send(text_update, "weekly")
    assert fake.sent[-1] == t("en", "err_invalid_repeat")  # the form waits for a valid repeat
    await send(text_update, "-")
    assert len(await _visits(session)) == 1


async def test_manager_reschedules_marks_done_and_cancels(env):
    session, fake, send, lift, _, make_visit = env
    v = await make_visit(lift, _at(3), repeat_every=30, repeat_unit=RepeatUnit.DAYS)

    new_time = _at(6, 14)
    await send(button_update, f"v:edit:{v.id}:visit_time")
    await send(text_update, _raw(new_time))
    assert v.scheduled_at == new_time and fake.last_to(OWNER) == t(
        "en", "notif_visit_moved", elevator="Lift 1", building="Tower A", when=_raw(new_time))
    await send(button_update, f"v:edit:{v.id}:visit_repeat")
    await send(text_update, "-")
    assert v.repeat_every is None

    await send(button_update, f"v:done:{v.id}")
    assert fake.sent[-1] == t("en", "confirm_visit_done", id=v.id)
    assert v.status is VisitStatus.SCHEDULED  # asking changes nothing
    await send(button_update, f"v:done_ok:{v.id}")
    assert v.status is VisitStatus.DONE and t("en", "vs_done") in fake.sent[-1]

    w = await make_visit(lift, _at(8), repeat_every=1, repeat_unit=RepeatUnit.MONTHS)
    await send(button_update, f"v:cx:{w.id}")
    await send(button_update, f"v:cx_ok:{w.id}")
    assert w.status is VisitStatus.CANCELLED
    assert len(await _visits(session)) == 3  # the monthly series continues
    assert fake.last_to(OWNER).startswith("🛠") and "cancelled" in fake.last_to(OWNER)


async def test_manager_lists(env):
    session, fake, send, lift, other_lift, make_visit = env
    await make_visit(lift, _at(3))
    await make_visit(other_lift, _at(4), VisitStatus.CHANGE_REQUESTED, customer_note="evening")
    await send(button_update, "v:menu")
    await send(button_update, "v:list:attention:0")
    assert fake.sent[-1] == t("en", "vg_attention") + " (1)"
    await send(button_update, "v:list:upcoming:0")
    assert fake.sent[-1] == t("en", "vg_upcoming") + " (1)"
    await send(button_update, "v:list:history:0")
    assert fake.sent[-1].startswith(t("en", "vg_history") + " (0)")


async def test_customer_confirms_from_the_reminder_buttons(env):
    session, fake, send, lift, _, make_visit = env
    v = await make_visit(lift, _at(1))
    await send(button_update, "v:list:mine:0", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "title_my_visits") + " (1)"
    await send(button_update, f"v:view:{v.id}", tg_id=OWNER)
    assert "Lift 1" in fake.sent[-1]
    await send(button_update, f"v:ok:{v.id}", tg_id=OWNER)
    assert v.status is VisitStatus.CONFIRMED
    assert t("en", "msg_visit_confirmed") in fake.sent
    assert fake.last_to(MANAGER) == t("en", "notif_visit_confirmed", customer="Owner", id=v.id,
                                      elevator="Lift 1", building="Tower A", when=_raw(v.scheduled_at))


async def test_customer_asks_for_another_time_with_a_note(env):
    session, fake, send, lift, _, make_visit = env
    v = await make_visit(lift, _at(2))
    await send(button_update, f"v:chg:{v.id}", tg_id=OWNER)
    assert fake.sent[-1].startswith(t("en", "prompt_change_note"))
    await send(text_update, "after 5 pm please", tg_id=OWNER)
    assert v.status is VisitStatus.CHANGE_REQUESTED and v.customer_note == "after 5 pm please"
    assert any(m.startswith(t("en", "msg_change_sent")) for m in fake.sent)
    assert "after 5 pm please" in fake.last_to(MANAGER)
    await send(button_update, f"v:chg:{v.id}", tg_id=OWNER)  # only once
    assert fake.sent[-1] == t("en", "err_visit_state")


async def test_customer_cancels_with_confirmation_and_the_series_continues(env):
    session, fake, send, lift, _, make_visit = env
    v = await make_visit(lift, _at(2), repeat_every=30, repeat_unit=RepeatUnit.DAYS)
    await send(button_update, f"v:cx:{v.id}", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "confirm_cancel_visit", id=v.id) and v.status is VisitStatus.SCHEDULED
    await send(button_update, f"v:cx_ok:{v.id}", tg_id=OWNER)
    assert v.status is VisitStatus.CANCELLED and len(await _visits(session)) == 2
    assert "cancelled" in fake.last_to(MANAGER)


async def test_access_rules(env):
    session, fake, send, lift, other_lift, make_visit = env
    mine = await make_visit(lift, _at(2))
    theirs = await make_visit(other_lift, _at(2))
    for tg in (OWNER, TECH):  # manager-only screens
        for data in ("v:menu", f"v:el:{lift.id}", f"v:add:{lift.id}"):
            await send(button_update, data, tg_id=tg)
            assert fake.sent[-1] == t("en", "no_permission"), (tg, data)
    for data in (f"v:done:{mine.id}", f"v:done_ok:{mine.id}", f"v:edit:{mine.id}:visit_time"):
        await send(button_update, data, tg_id=OWNER)
        assert fake.sent[-1] == t("en", "no_permission"), data
    for data in (f"v:view:{theirs.id}", f"v:ok:{theirs.id}", f"v:cx:{theirs.id}", f"v:cx_ok:{theirs.id}", f"v:chg:{theirs.id}"):
        await send(button_update, data, tg_id=OWNER)
        assert fake.sent[-1] == t("en", "err_not_found"), data
    await send(button_update, f"v:ok:{mine.id}", tg_id=TECH)
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"v:ok:{mine.id}")  # managers do not use the customer buttons
    assert fake.sent[-1] == t("en", "no_permission")
    assert mine.status is VisitStatus.SCHEDULED and theirs.status is VisitStatus.SCHEDULED


async def test_garbled_callbacks_are_rejected_politely(env):
    _, fake, send, *_ = env
    for data in ("v:view:abc", "v:list:upcoming", "v:list:upcoming:x", "v:el:", "v:edit:1", "v:edit:1:bogus"):
        await send(button_update, data)
        assert fake.sent[-1] == t("en", "err_invalid_text"), data
