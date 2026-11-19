import pytest
from sqlalchemy import select

from app.models import (
    AuditLog, Building, Company, Elevator, ReviewStatus, Role, TicketStatus, TicketStatusHistory, User,
)
from app.services.directory import DirectoryService
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from app.services.tickets import TicketService

S = TicketStatus


async def _user(session, company, role, phone, **kw):
    user = User(company_id=company.id, phone=phone, name=kw.pop("name", role.value), role=role, **kw)
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    manager = await _user(session, company, Role.MANAGER, "+9647500000001")
    customer = await _user(session, company, Role.CUSTOMER, "+9647500000002", name="Ali")
    tech = await _user(session, company, Role.TECHNICIAN, "+9647500000003", name="Tek")
    tech2 = await _user(session, company, Role.TECHNICIAN, "+9647500000004", name="Tek2")
    building = Building(company_id=company.id, owner_id=customer.id, name="Tower", address="Erbil")
    session.add(building)
    await session.flush()
    elevator = Elevator(company_id=company.id, building_id=building.id, name="E1")
    session.add(elevator)
    await session.flush()

    def svc(actor):
        return TicketService(session, company, actor)

    return dict(session=session, company=company, manager=manager, customer=customer, tech=tech,
                tech2=tech2, building=building, elevator=elevator, svc=svc)


async def _history(session, ticket):
    rows = await session.execute(
        select(TicketStatusHistory).where(TicketStatusHistory.ticket_id == ticket.id).order_by(TicketStatusHistory.id)
    )
    return [(h.from_status, h.to_status) for h in rows.scalars()]


async def _reported_ticket(w):
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "stuck", "FILE1")
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    await w["svc"](w["tech"]).start_work(t.id)
    await w["svc"](w["tech"]).submit_report(t.id, "replaced the door sensor")
    return t


async def test_customer_creates_ticket_with_photo_and_history(world):
    w = world
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "  door stuck ", "FILE1")
    assert t.status is S.NEW and t.description == "door stuck" and t.customer_id == w["customer"].id
    assert await _history(w["session"], t) == [(None, S.NEW)]
    details = await w["svc"](w["customer"]).get_details(t.id)
    assert details.photos == ["FILE1"]


async def test_manager_reports_on_behalf_of_customer(world):
    w = world
    t = await w["svc"](w["manager"]).create_ticket(w["elevator"].id, "noise", customer_id=w["customer"].id)
    assert t.customer_id == w["customer"].id
    event = (await w["session"].execute(select(AuditLog).where(AuditLog.event_type == "ticket_created"))).scalar_one()
    assert event.details == {"on_behalf": True}
    with pytest.raises(NotFound):  # missing / wrong customer
        await w["svc"](w["manager"]).create_ticket(w["elevator"].id, "x")
    with pytest.raises(PermissionDenied):
        await w["svc"](w["tech"]).create_ticket(w["elevator"].id, "x")


async def test_customer_can_only_report_own_active_elevators(world):
    w = world
    other = await _user(w["session"], w["company"], Role.CUSTOMER, "+9647500000009")
    with pytest.raises(NotFound):
        await w["svc"](other).create_ticket(w["elevator"].id, "not mine")
    assert await w["svc"](other).reportable_elevators() == []
    assert len(await w["svc"](w["customer"]).reportable_elevators()) == 1
    w["elevator"].is_active = False
    assert await w["svc"](w["customer"]).reportable_elevators() == []
    with pytest.raises(InvalidInput):
        await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    w["elevator"].is_active = True
    with pytest.raises(InvalidInput):
        await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "   ")


async def test_full_lifecycle_to_closed(world):
    w = world
    t = await _reported_ticket(w)
    assert t.status is S.REPORTED and t.technician_id == w["tech"].id
    with pytest.raises(InvalidInput):  # close needs an approved report
        await w["svc"](w["manager"]).close(t.id)
    await w["svc"](w["manager"]).approve_report(t.id)
    await w["svc"](w["manager"]).close(t.id)
    assert t.status is S.CLOSED and t.closed_by_id == w["manager"].id and t.closed_at
    assert await _history(w["session"], t) == [
        (None, S.NEW), (S.NEW, S.ASSIGNED), (S.ASSIGNED, S.IN_PROGRESS),
        (S.IN_PROGRESS, S.REPORTED), (S.REPORTED, S.CLOSED),
    ]
    events = (await w["session"].execute(select(AuditLog.event_type))).scalars().all()
    assert {"ticket_assigned", "report_approved", "ticket_closed"} <= set(events)
    with pytest.raises(InvalidInput):  # terminal
        await w["svc"](w["manager"]).cancel(t.id)


async def test_reject_returns_to_in_progress_and_allows_second_report(world):
    w = world
    t = await _reported_ticket(w)
    await w["svc"](w["manager"]).reject_report(t.id, "Photos of the repair, please")
    assert t.status is S.IN_PROGRESS
    details = await w["svc"](w["tech"]).get_details(t.id)
    assert details.latest_report.review_status is ReviewStatus.REJECTED
    assert details.latest_report.review_note == "Photos of the repair, please"
    await w["svc"](w["tech"]).submit_report(t.id, "second report")
    assert t.status is S.REPORTED
    assert len((await w["svc"](w["manager"]).get_details(t.id)).reports) == 2
    with pytest.raises(InvalidInput):  # note is required
        await w["svc"](w["manager"]).reject_report(t.id, " ")


async def test_reassign_rules(world):
    w = world
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    mgr = w["svc"](w["manager"])
    with pytest.raises(InvalidInput):
        await mgr.assign(t.id, w["customer"].id)  # not a technician
    await mgr.assign(t.id, w["tech"].id)
    with pytest.raises(InvalidInput):
        await mgr.assign(t.id, w["tech"].id)  # same technician
    await w["svc"](w["tech"]).start_work(t.id)
    await mgr.assign(t.id, w["tech2"].id)  # change while in progress
    assert t.status is S.ASSIGNED and t.technician_id == w["tech2"].id and t.started_at is None
    with pytest.raises(NotFound):  # the previous technician loses access
        await w["svc"](w["tech"]).get_details(t.id)
    w["tech"].is_active = False
    with pytest.raises(InvalidInput):  # inactive technician cannot be assigned
        await mgr.assign(t.id, w["tech"].id)


async def test_cannot_reassign_after_report(world):
    w = world
    t = await _reported_ticket(w)
    with pytest.raises(InvalidInput):
        await w["svc"](w["manager"]).assign(t.id, w["tech2"].id)


async def test_only_the_assigned_technician_can_start_and_report(world):
    w = world
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    with pytest.raises(NotFound):
        await w["svc"](w["tech2"]).start_work(t.id)
    with pytest.raises(PermissionDenied):
        await w["svc"](w["manager"]).start_work(t.id)
    with pytest.raises(InvalidInput):  # must start first
        await w["svc"](w["tech"]).submit_report(t.id, "done")
    await w["svc"](w["tech"]).start_work(t.id)
    with pytest.raises(InvalidInput):  # already started
        await w["svc"](w["tech"]).start_work(t.id)


async def test_only_managers_can_assign_review_close_cancel(world):
    w = world
    t = await _reported_ticket(w)
    for actor in (w["customer"], w["tech"]):
        s = w["svc"](actor)
        for call in (s.approve_report(t.id), s.close(t.id), s.cancel(t.id), s.reject_report(t.id, "n"),
                     s.assign(t.id, w["tech2"].id)):
            with pytest.raises(PermissionDenied):
                await call


async def test_cancel_from_open_statuses(world):
    w = world
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    await w["svc"](w["manager"]).cancel(t.id)
    assert t.status is S.CANCELLED and t.closed_at is None


async def test_visibility_and_customer_never_sees_reports(world):
    w = world
    t = await _reported_ticket(w)
    other = await _user(w["session"], w["company"], Role.CUSTOMER, "+9647500000009")
    with pytest.raises(NotFound):
        await w["svc"](other).get_details(t.id)
    with pytest.raises(NotFound):
        await w["svc"](w["tech2"]).get_details(t.id)
    assert (await w["svc"](w["customer"]).get_details(t.id)).reports == []
    assert len((await w["svc"](w["tech"]).get_details(t.id)).reports) == 1
    foreign_company = Company(name="Other")
    w["session"].add(foreign_company)
    await w["session"].flush()
    foreign_mgr = await _user(w["session"], foreign_company, Role.MANAGER, "+9647509990001")
    with pytest.raises(NotFound):
        await TicketService(w["session"], foreign_company, foreign_mgr).get_details(t.id)


async def test_lists_and_counts(world):
    w = world
    a = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "a")
    b = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "b")
    await w["svc"](w["manager"]).assign(b.id, w["tech"].id)
    assert await w["svc"](w["manager"]).counts() == {"new": 1, "active": 1, "review": 0, "done": 0}
    rows, total = await w["svc"](w["manager"]).list_tickets("new", 0)
    assert total == 1 and rows[0].ticket.id == a.id and rows[0].elevator.name == "E1"
    mine, total = await w["svc"](w["customer"]).list_tickets("mine", 0)
    assert total == 2 and [r.ticket.id for r in mine] == [b.id, a.id]  # newest first
    tasks, total = await w["svc"](w["tech"]).list_tickets("tasks", 0)
    assert total == 1 and tasks[0].ticket.id == b.id
    with pytest.raises(PermissionDenied):
        await w["svc"](w["customer"]).list_tickets("new", 0)
    with pytest.raises(PermissionDenied):
        await w["svc"](w["tech"]).counts()


async def test_technician_with_open_tickets_cannot_be_deactivated(world):
    w = world
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    directory = DirectoryService(w["session"], w["company"], w["manager"])
    with pytest.raises(Conflict):
        await directory.set_user_active(w["tech"].id, False)
    await w["svc"](w["manager"]).cancel(t.id)
    await directory.set_user_active(w["tech"].id, False)  # free once the ticket is finished
    assert w["tech"].is_active is False
