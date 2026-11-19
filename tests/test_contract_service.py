"""Contracts of buildings: validation, overlap, visibility, renewal requests."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.outbox import Outbox
from app.models import AuditLog, Building, Company, Contract, ContractReminder, ContractStatus, Role, User
from app.services.contracts import ContractService
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied

TODAY = date(2026, 11, 14)


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg=None, **kw):
        return User(company_id=company.id, phone=phone, name=f"U{phone[-2:]}", role=role,
                    telegram_id=tg, language="en", **kw)

    boss = user("+9647500000001", Role.MANAGER, 101)
    other_boss = user("+9647500000002", Role.MANAGER, 102)
    owner = user("+9647500000003", Role.CUSTOMER, 103)
    stranger = user("+9647500000004", Role.CUSTOMER, 104)
    tech = user("+9647500000005", Role.TECHNICIAN, 105)
    session.add_all([boss, other_boss, owner, stranger, tech])
    await session.flush()
    b1 = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    b2 = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([b1, b2])
    await session.flush()
    return company, boss, other_boss, owner, stranger, tech, b1, b2


def _svc(session, company, actor, box=None):
    return ContractService(session, company, actor, box)


async def test_manager_creates_contract_and_it_is_audited(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    c = await svc.create(b1.id, "2026-01-01", "2026-12-31", "yearly")
    assert c.status is ContractStatus.ACTIVE and c.end_date == date(2026, 12, 31) and c.notes == "yearly"
    entry = (await session.execute(select(AuditLog).where(AuditLog.event_type == "contract_created"))).scalar_one()
    assert entry.entity_id == c.id and entry.actor_id == boss.id


async def test_only_managers_and_customers_may_use_the_service(session, world):
    company, boss, _, owner, _, tech, b1, _ = world
    with pytest.raises(PermissionDenied):
        _svc(session, company, tech)
    with pytest.raises(PermissionDenied):
        _svc(session, company, None)
    with pytest.raises(PermissionDenied):  # a customer cannot create, edit or cancel
        await _svc(session, company, owner).create(b1.id, "2026-01-01", "2026-12-31")
    c = await _svc(session, company, boss).create(b1.id, "2026-01-01", "2026-12-31")
    for action in (lambda s: s.update_field(c.id, "contract_notes", "x"), lambda s: s.cancel(c.id),
                   lambda s: s.counts(), lambda s: s.list_for_building(b1.id)):
        with pytest.raises(PermissionDenied):
            await action(_svc(session, company, owner))


async def test_date_validation(session, world):
    company, boss, _, _, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    with pytest.raises(InvalidInput) as bad:
        await svc.create(b1.id, "2026-05-01", "2026-04-30")
    assert bad.value.key == "err_contract_dates"
    with pytest.raises(InvalidInput) as fmt:
        await svc.create(b1.id, "31/12/2026", "2027-01-01")
    assert fmt.value.key == "err_invalid_date"
    with pytest.raises(InvalidInput):  # both dates are required
        await svc.create(b1.id, "2026-05-01", "-")
    one_day = await svc.create(b1.id, "2026-05-01", "2026-05-01")  # same day is allowed
    assert one_day.start_date == one_day.end_date


async def test_overlap_in_one_building_is_refused_but_other_buildings_and_cancelled_are_fine(session, world):
    company, boss, _, _, _, _, b1, b2 = world
    svc = _svc(session, company, boss)
    first = await svc.create(b1.id, "2026-01-01", "2026-06-30")
    with pytest.raises(Conflict) as exc:
        await svc.create(b1.id, "2026-06-30", "2026-12-31")  # touches the last day
    assert exc.value.key == "err_contract_overlap"
    await svc.create(b1.id, "2026-07-01", "2026-12-31")  # the day after is fine (renewal)
    await svc.create(b2.id, "2026-01-01", "2026-06-30")  # another building
    await svc.cancel(first.id)
    await svc.create(b1.id, "2026-02-01", "2026-03-01")  # cancelled contracts do not block


async def test_inactive_building_and_unknown_building(session, world):
    company, boss, _, _, _, _, b1, _ = world
    b1.is_active = False
    await session.flush()
    svc = _svc(session, company, boss)
    with pytest.raises(InvalidInput) as exc:
        await svc.create(b1.id, "2026-01-01", "2026-12-31")
    assert exc.value.key == "err_inactive_parent"
    with pytest.raises(NotFound):
        await svc.create(999999, "2026-01-01", "2026-12-31")


async def test_changing_end_date_restarts_reminders_and_settles_renewal(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    c = await svc.create(b1.id, "2026-01-01", "2026-12-01")
    session.add(ContractReminder(company_id=company.id, contract_id=c.id, days_before=30,
                                 sent_at=datetime.now(timezone.utc)))
    c.renewal_requested_at = datetime.now(timezone.utc)
    await session.flush()

    await svc.update_field(c.id, "end_date", "2027-12-01")
    assert c.end_date == date(2027, 12, 1) and c.renewal_requested_at is None
    left = (await session.execute(select(ContractReminder).where(ContractReminder.contract_id == c.id))).all()
    assert left == []
    with pytest.raises(InvalidInput) as exc:  # not before the start date
        await svc.update_field(c.id, "end_date", "2025-12-01")
    assert exc.value.key == "err_contract_dates"
    with pytest.raises(InvalidInput):
        await svc.update_field(c.id, "start_date", "2026-02-01")  # start date is not editable
    await svc.update_field(c.id, "contract_notes", "-")  # optional field can be cleared
    assert c.notes is None


async def test_extending_into_another_contract_is_refused(session, world):
    company, boss, _, _, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    first = await svc.create(b1.id, "2026-01-01", "2026-06-30")
    await svc.create(b1.id, "2026-07-01", "2026-12-31")
    with pytest.raises(Conflict):
        await svc.update_field(first.id, "end_date", "2026-08-01")


async def test_cancel_is_final(session, world):
    company, boss, _, _, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    c = await svc.create(b1.id, "2026-01-01", "2026-12-31")
    await svc.cancel(c.id)
    assert c.status is ContractStatus.CANCELLED
    for action in (svc.cancel(c.id), svc.update_field(c.id, "contract_notes", "x")):
        with pytest.raises(InvalidInput) as exc:
            await action
        assert exc.value.key == "err_contract_state"


async def test_customers_see_only_their_own_contracts(session, world):
    company, boss, _, owner, stranger, _, b1, b2 = world
    mine = await _svc(session, company, boss).create(b1.id, "2026-01-01", "2026-12-31")
    theirs = await _svc(session, company, boss).create(b2.id, "2026-01-01", "2026-12-31")
    svc = _svc(session, company, owner)
    rows, total = await svc.list_contracts("mine", 0, TODAY)
    assert total == 1 and [c.id for c, _ in rows] == [mine.id]
    assert (await svc.get_details(mine.id)).owner.id == owner.id
    with pytest.raises(NotFound):
        await svc.get_details(theirs.id)
    with pytest.raises(NotFound):
        await _svc(session, company, owner).request_renewal(theirs.id, TODAY)


async def test_manager_groups_and_counts(session, world):
    company, boss, _, _, _, _, b1, b2 = world
    svc = _svc(session, company, boss)
    soon = await svc.create(b1.id, "2026-01-01", "2026-11-20")      # 6 days left
    later = await svc.create(b2.id, "2026-01-01", "2027-03-01")      # active, not expiring
    past = await svc.create(b2.id, "2025-01-01", "2025-12-31")       # already over
    gone = await svc.create(b1.id, "2026-12-01", "2026-12-31")
    await svc.cancel(gone.id)

    assert await svc.counts(TODAY) == {"expiring": 1, "active": 2, "ended": 2}
    expiring, _ = await svc.list_contracts("expiring", 0, TODAY)
    assert [c.id for c, _ in expiring] == [soon.id]
    active, _ = await svc.list_contracts("active", 0, TODAY)
    assert [c.id for c, _ in active] == [soon.id, later.id]  # soonest end first
    ended, _ = await svc.list_contracts("ended", 0, TODAY)
    assert {c.id for c, _ in ended} == {past.id, gone.id}
    with pytest.raises(InvalidInput):
        await svc.list_contracts("mine", 0, TODAY)  # customer-only group
    building, items = await svc.list_for_building(b2.id)
    assert building.id == b2.id and {c.id for c in items} == {later.id, past.id}


async def test_renewal_request_notifies_managers_and_is_limited(session, world):
    company, boss, other_boss, owner, _, _, b1, _ = world
    c = await _svc(session, company, boss).create(b1.id, "2026-01-01", "2026-12-31")
    box = Outbox()
    svc = _svc(session, company, owner, box)
    await svc.request_renewal(c.id, TODAY)

    assert {n.telegram_id for n in box.items} == {101, 102}
    n = box.items[0]
    assert n.key == "notif_renewal_requested" and n.contract_id == c.id
    assert n.params == {"id": c.id, "customer": owner.name, "building": "Tower A", "end": "2026-12-31"}
    with pytest.raises(Conflict) as exc:  # a second request within 24 hours
        await svc.request_renewal(c.id, TODAY)
    assert exc.value.key == "err_renewal_recent"
    await session.execute(update(Contract).values(renewal_requested_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    await session.refresh(c)
    await svc.request_renewal(c.id, TODAY)  # allowed again


async def test_renewal_request_rules(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    manager_svc = _svc(session, company, boss)
    c = await manager_svc.create(b1.id, "2026-01-01", "2026-12-31")
    with pytest.raises(PermissionDenied):  # only the customer asks
        await manager_svc.request_renewal(c.id, TODAY)
    customer_svc = _svc(session, company, owner)
    with pytest.raises(InvalidInput):  # already over
        await customer_svc.request_renewal(c.id, date(2027, 1, 1))
    await manager_svc.cancel(c.id)
    with pytest.raises(InvalidInput):  # cancelled
        await customer_svc.request_renewal(c.id, TODAY)
