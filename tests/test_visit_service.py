"""Service visits: validation, repeat rule, customer reactions, notifications, visibility."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.outbox import Outbox
from app.core.timeutil import LOCAL_TZ
from app.models import AuditLog, Building, Company, Elevator, RepeatUnit, Role, ServiceVisit, User, VisitStatus
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.validation import validate_field
from app.services.visits import VisitService, add_months, next_time


def _local(days: float = 0, hour: int = 9, minute: int = 0) -> datetime:
    """A local-time instant `days` from today at hour:minute."""
    base = datetime.now(timezone.utc).astimezone(LOCAL_TZ) + timedelta(days=days)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _raw(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg=None, name=None, **kw):
        return User(company_id=company.id, phone=phone, name=name or f"U{phone[-2:]}", role=role,
                    telegram_id=tg, language="en", **kw)

    boss = user("+9647500000001", Role.MANAGER, 101, "Boss")
    other_boss = user("+9647500000002", Role.MANAGER, 102, "Boss2")
    owner = user("+9647500000003", Role.CUSTOMER, 103, "Owner")
    stranger = user("+9647500000004", Role.CUSTOMER, 104, "Stranger")
    tech = user("+9647500000005", Role.TECHNICIAN, 105)
    session.add_all([boss, other_boss, owner, stranger, tech])
    await session.flush()
    b1 = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    b2 = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([b1, b2])
    await session.flush()
    e1 = Elevator(company_id=company.id, building_id=b1.id, name="Lift 1")
    e2 = Elevator(company_id=company.id, building_id=b2.id, name="Lift 2")
    session.add_all([e1, e2])
    await session.flush()
    return company, boss, owner, stranger, tech, e1, e2, b1


def _svc(session, company, actor, box=None):
    return VisitService(session, company, actor, box)


# ---- validation and arithmetic ----


def test_datetime_and_repeat_validation():
    when = validate_field("visit_time", "2026-12-01 09:30")
    assert when == datetime(2026, 12, 1, 9, 30, tzinfo=LOCAL_TZ)
    for bad in ("2026-12-01", "01/12/2026 09:30", "2026-13-01 09:30", "2026-12-01 25:00"):
        with pytest.raises(InvalidInput) as exc:
            validate_field("visit_time", bad)
        assert exc.value.key == "err_invalid_datetime"
    assert validate_field("visit_repeat", "30d") == (30, RepeatUnit.DAYS)
    assert validate_field("visit_repeat", " 3M ") == (3, RepeatUnit.MONTHS)
    assert validate_field("visit_repeat", "-") is None
    for bad in ("0d", "366d", "25m", "d", "30", "1w", "-5d"):
        with pytest.raises(InvalidInput) as exc:
            validate_field("visit_repeat", bad)
        assert exc.value.key == "err_invalid_repeat", bad


def test_add_months_clamps_to_month_end():
    jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=LOCAL_TZ)
    assert add_months(jan31, 1) == datetime(2026, 2, 28, 9, 0, tzinfo=LOCAL_TZ)
    assert add_months(jan31, 2) == datetime(2026, 3, 31, 9, 0, tzinfo=LOCAL_TZ)
    assert add_months(datetime(2026, 11, 15, 9, 0, tzinfo=LOCAL_TZ), 3) == datetime(2027, 2, 15, 9, 0, tzinfo=LOCAL_TZ)
    assert add_months(datetime(2026, 12, 5, 9, 0, tzinfo=LOCAL_TZ), 12) == datetime(2027, 12, 5, 9, 0, tzinfo=LOCAL_TZ)


def test_next_time_skips_dates_in_the_past():
    visit = ServiceVisit(scheduled_at=datetime(2026, 1, 10, 9, 0, tzinfo=LOCAL_TZ), repeat_every=30, repeat_unit=RepeatUnit.DAYS)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert next_time(visit, now) == datetime(2026, 3, 11, 9, 0, tzinfo=LOCAL_TZ)  # 10 Jan + 60 days
    assert next_time(ServiceVisit(scheduled_at=visit.scheduled_at), now) is None  # no repeat rule


# ---- creating ----


async def test_manager_creates_a_visit_and_the_customer_is_told(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    box = Outbox()
    when = _local(10)
    visit = await _svc(session, company, boss, box).create(e1.id, _raw(when), "3m")

    assert visit.status is VisitStatus.SCHEDULED and visit.scheduled_at == when
    assert (visit.repeat_every, visit.repeat_unit) == (3, RepeatUnit.MONTHS)
    [n] = box.items
    assert (n.telegram_id, n.key, n.visit_id) == (103, "notif_visit_scheduled", visit.id)
    assert n.params["elevator"] == "Lift 1" and n.params["building"] == "Tower A" and n.params["when"] == _raw(when)
    entry = (await session.execute(select(AuditLog).where(AuditLog.event_type == "visit_created"))).scalar_one()
    assert entry.entity_id == visit.id and entry.actor_id == boss.id


async def test_create_rules(session, world):
    company, boss, owner, _, tech, e1, _, b1 = world
    svc = _svc(session, company, boss)
    with pytest.raises(InvalidInput) as past:
        await svc.create(e1.id, _raw(_local(-1)))
    assert past.value.key == "err_visit_past"
    with pytest.raises(InvalidInput):
        await svc.create(e1.id, "-")  # the time is required
    with pytest.raises(InvalidInput) as bad:
        await svc.create(e1.id, _raw(_local(3)), "weekly")
    assert bad.value.key == "err_invalid_repeat"
    with pytest.raises(NotFound):
        await svc.create(999999, _raw(_local(3)))
    with pytest.raises(PermissionDenied):  # a customer cannot plan visits
        await _svc(session, company, owner).create(e1.id, _raw(_local(3)))
    for actor in (tech, None):  # technicians and unknown users cannot use the service at all
        with pytest.raises(PermissionDenied):
            _svc(session, company, actor)
    e1.is_active = False
    await session.flush()
    with pytest.raises(InvalidInput) as inactive:
        await svc.create(e1.id, _raw(_local(3)))
    assert inactive.value.key == "err_inactive_parent"
    e1.is_active, b1.is_active = True, False
    await session.flush()
    with pytest.raises(InvalidInput):
        await svc.create(e1.id, _raw(_local(3)))


async def test_unreachable_customer_gets_no_message_but_the_visit_is_created(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    owner.telegram_id = None
    await session.flush()
    box = Outbox()
    await _svc(session, company, boss, box).create(e1.id, _raw(_local(3)))
    assert box.items == []


# ---- manager changes ----


async def test_reschedule_resets_the_customer_decision_and_reminder(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    manager = _svc(session, company, boss)
    visit = await manager.create(e1.id, _raw(_local(3)))
    visit.status, visit.customer_note = VisitStatus.CHANGE_REQUESTED, "after 5pm"
    visit.reminder_sent_at = datetime.now(timezone.utc)
    box = Outbox()
    new_time = _local(8, 17, 30)
    await _svc(session, company, boss, box).reschedule(visit.id, _raw(new_time))
    assert visit.status is VisitStatus.SCHEDULED and visit.scheduled_at == new_time
    assert visit.reminder_sent_at is None and visit.customer_note is None
    assert [n.key for n in box.items] == ["notif_visit_moved"] and box.items[0].params["when"] == _raw(new_time)
    with pytest.raises(InvalidInput) as past:
        await manager.reschedule(visit.id, _raw(_local(-2)))
    assert past.value.key == "err_visit_past"


async def test_set_and_remove_repeat(session, world):
    company, boss, _, _, _, e1, _, _ = world
    svc = _svc(session, company, boss)
    visit = await svc.create(e1.id, _raw(_local(3)))
    await svc.set_repeat(visit.id, "30d")
    assert (visit.repeat_every, visit.repeat_unit) == (30, RepeatUnit.DAYS)
    await svc.set_repeat(visit.id, "-")
    assert visit.repeat_every is None and visit.repeat_unit is None


async def test_done_creates_the_next_visit_by_the_repeat_rule(session, world):
    company, boss, _, _, _, e1, _, _ = world
    svc = _svc(session, company, boss)
    first_time = _local(2)
    visit = await svc.create(e1.id, _raw(first_time), "30d")
    done, nxt = await svc.mark_done(visit.id)
    assert done.status is VisitStatus.DONE and done.done_at is not None
    assert nxt.status is VisitStatus.SCHEDULED and nxt.scheduled_at == first_time + timedelta(days=30)
    assert (nxt.repeat_every, nxt.repeat_unit) == (30, RepeatUnit.DAYS)
    with pytest.raises(InvalidInput) as exc:  # closed visits cannot be changed
        await svc.mark_done(visit.id)
    assert exc.value.key == "err_visit_state"
    for action in (svc.reschedule(visit.id, _raw(_local(5))), svc.set_repeat(visit.id, "-"), svc.cancel(visit.id)):
        with pytest.raises(InvalidInput):
            await action


async def test_done_without_repeat_creates_nothing(session, world):
    company, boss, _, _, _, e1, _, _ = world
    svc = _svc(session, company, boss)
    visit = await svc.create(e1.id, _raw(_local(2)))
    _, nxt = await svc.mark_done(visit.id)
    assert nxt is None
    assert len((await session.execute(select(ServiceVisit))).all()) == 1


async def test_late_done_skips_missed_dates(session, world):
    company, boss, _, _, _, e1, _, _ = world
    svc = _svc(session, company, boss)
    visit = await svc.create(e1.id, _raw(_local(2)), "7d")
    visit.scheduled_at = _local(-20)  # three weeks late
    _, nxt = await svc.mark_done(visit.id)
    assert nxt.scheduled_at > datetime.now(timezone.utc)
    assert nxt.scheduled_at == _local(-20) + timedelta(days=21)  # 3 steps of 7 days: the first one in the future


async def test_next_visit_is_not_duplicated_or_created_for_inactive_elevators(session, world):
    company, boss, _, _, _, e1, _, _ = world
    svc = _svc(session, company, boss)
    a = await svc.create(e1.id, _raw(_local(2)), "30d")
    clash = await svc.create(e1.id, _raw(_local(2) + timedelta(days=30)))  # the date the next one would take
    _, nxt = await svc.mark_done(a.id)
    assert nxt is None and clash.status is VisitStatus.SCHEDULED
    b = await svc.create(e1.id, _raw(_local(40)), "30d")
    e1.is_active = False
    await session.flush()
    _, nxt = await svc.mark_done(b.id)
    assert nxt is None


async def test_manager_cancel_tells_the_customer_and_continues_the_series(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    visit = await _svc(session, company, boss).create(e1.id, _raw(_local(2)), "3m")
    box = Outbox()
    cancelled, nxt = await _svc(session, company, boss, box).cancel(visit.id)
    assert cancelled.status is VisitStatus.CANCELLED and nxt is not None
    assert [(n.telegram_id, n.key) for n in box.items] == [(103, "notif_visit_cancelled")]


# ---- customer reactions ----


async def test_customer_confirms_and_managers_are_told(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    visit = await _svc(session, company, boss).create(e1.id, _raw(_local(2)))
    box = Outbox()
    await _svc(session, company, owner, box).confirm(visit.id)
    assert visit.status is VisitStatus.CONFIRMED
    assert {n.telegram_id for n in box.items} == {101, 102}
    assert box.items[0].key == "notif_visit_confirmed" and box.items[0].params["customer"] == "Owner"
    with pytest.raises(InvalidInput):  # already confirmed
        await _svc(session, company, owner).confirm(visit.id)


async def test_customer_asks_for_another_time(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    visit = await _svc(session, company, boss).create(e1.id, _raw(_local(2)))
    box = Outbox()
    await _svc(session, company, owner, box).request_change(visit.id, "after 5 pm")
    assert visit.status is VisitStatus.CHANGE_REQUESTED and visit.customer_note == "after 5 pm"
    assert box.items[0].key == "notif_visit_change_requested" and box.items[0].params["note"] == "after 5 pm"
    with pytest.raises(InvalidInput):  # only once, until the manager moves it
        await _svc(session, company, owner).request_change(visit.id, "x")
    other = await _svc(session, company, boss).create(e1.id, _raw(_local(5)))
    await _svc(session, company, owner, Outbox()).request_change(other.id, "-")  # the note is optional
    assert other.customer_note is None
    with pytest.raises(InvalidInput) as long:
        third = await _svc(session, company, boss).create(e1.id, _raw(_local(6)))
        await _svc(session, company, owner).request_change(third.id, "x" * 501)
    assert long.value.key == "err_text_too_long"


async def test_customer_cancel_continues_the_series_and_tells_managers(session, world):
    company, boss, owner, _, _, e1, _, _ = world
    visit = await _svc(session, company, boss).create(e1.id, _raw(_local(2)), "30d")
    box = Outbox()
    cancelled, nxt = await _svc(session, company, owner, box).cancel(visit.id)
    assert cancelled.status is VisitStatus.CANCELLED and nxt is not None
    assert {(n.telegram_id, n.key) for n in box.items} == {(101, "notif_visit_cancelled_by_customer"), (102, "notif_visit_cancelled_by_customer")}


async def test_customer_cannot_react_to_a_past_visit_or_someone_elses(session, world):
    company, boss, owner, stranger, _, e1, e2, _ = world
    manager = _svc(session, company, boss)
    old = await manager.create(e1.id, _raw(_local(2)))
    old.scheduled_at = _local(-1)
    theirs = await manager.create(e2.id, _raw(_local(2)))
    svc = _svc(session, company, owner)
    for action in (svc.confirm(old.id), svc.request_change(old.id, "x"), svc.cancel(old.id)):
        with pytest.raises(InvalidInput):
            await action
    for action in (svc.confirm(theirs.id), svc.cancel(theirs.id), svc.request_change(theirs.id, "x"), svc.get_details(theirs.id)):
        with pytest.raises(NotFound):
            await action
    for action in (svc.mark_done(old.id), svc.reschedule(old.id, _raw(_local(3))), svc.set_repeat(old.id, "-")):
        with pytest.raises(PermissionDenied):
            await action
    with pytest.raises(PermissionDenied):  # managers do not use the customer buttons
        await manager.confirm(old.id)


# ---- lists ----


async def test_manager_groups_and_customer_list(session, world):
    company, boss, owner, stranger, _, e1, e2, _ = world
    m = _svc(session, company, boss)
    soon = await m.create(e1.id, _raw(_local(3)))
    later = await m.create(e1.id, _raw(_local(20)))
    asked = await m.create(e2.id, _raw(_local(6)))
    asked.status = VisitStatus.CHANGE_REQUESTED
    late = await m.create(e1.id, _raw(_local(9)))
    late.scheduled_at = _local(-2)
    closed = await m.create(e1.id, _raw(_local(11)))
    await m.cancel(closed.id)
    await session.flush()

    assert await m.counts() == {"attention": 2, "upcoming": 2, "history": 1}
    attention, _ = await m.list_visits("attention", 0)
    assert [v.id for v, _, _ in attention] == [late.id, asked.id]  # earliest time first
    upcoming, _ = await m.list_visits("upcoming", 0)
    assert [v.id for v, _, _ in upcoming] == [soon.id, later.id]
    history, _ = await m.list_visits("history", 0)
    assert [v.id for v, _, _ in history] == [closed.id]
    with pytest.raises(InvalidInput):
        await m.list_visits("mine", 0)

    mine, total = await _svc(session, company, owner).list_visits("mine", 0)
    assert total == 3 and [v.id for v, _, _ in mine] == [late.id, soon.id, later.id]  # only open, own elevators
    elevator, building, visits = await m.list_for_elevator(e1.id)
    assert elevator.id == e1.id and {v.id for v in visits} == {soon.id, later.id, late.id, closed.id}
