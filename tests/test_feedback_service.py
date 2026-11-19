import pytest
from sqlalchemy import select

from app.core.outbox import Outbox
from app.models import AuditLog, Building, Company, Elevator, Role, TicketFeedback, TicketStatus, User
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.tickets import TicketService


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
    manager = await _user(session, company, Role.MANAGER, "+9647500000001", telegram_id=1, language="en")
    manager2 = await _user(session, company, Role.MANAGER, "+9647500000011", telegram_id=11, language="fa")
    customer = await _user(session, company, Role.CUSTOMER, "+9647500000002", telegram_id=2, language="ckb", name="Ali")
    tech = await _user(session, company, Role.TECHNICIAN, "+9647500000003", telegram_id=3, language="en")
    building = Building(company_id=company.id, owner_id=customer.id, name="Tower", address="Erbil")
    session.add(building)
    await session.flush()
    elevator = Elevator(company_id=company.id, building_id=building.id, name="E1")
    session.add(elevator)
    await session.flush()
    box = Outbox()

    def svc(actor):
        return TicketService(session, company, actor, outbox=box)

    return dict(session=session, company=company, manager=manager, manager2=manager2, customer=customer,
                tech=tech, elevator=elevator, box=box, svc=svc)


def _sent(box):
    return [(n.telegram_id, n.key, n.action) for n in box.items]


async def _reported(w):
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "stuck")
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    await w["svc"](w["tech"]).start_work(t.id)
    await w["svc"](w["tech"]).submit_report(t.id, "fixed")
    return t


async def test_notifications_follow_the_ticket_flow(world):
    w, box = world, world["box"]
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x" * 400)
    assert _sent(box) == [(1, "notif_new_ticket", "view"), (11, "notif_new_ticket", "view")]
    first = box.items[0]
    assert first.language == "en" and box.items[1].language == "fa"  # each in their own language
    assert first.params["customer"] == "Ali" and first.params["building"] == "Tower"
    assert first.params["id"] == t.id and len(first.params["description"]) == 300
    box.clear()

    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    assert _sent(box) == [(2, "notif_status_assigned", "view"), (3, "notif_assigned", "view")]
    box.clear()

    await w["svc"](w["tech"]).start_work(t.id)
    assert _sent(box) == [(2, "notif_status_in_progress", "view")]  # only the customer follows this
    box.clear()
    await w["svc"](w["tech"]).submit_report(t.id, "fixed")
    assert _sent(box) == [(2, "notif_status_reported", "view"), (1, "notif_report_submitted", "view"),
                          (11, "notif_report_submitted", "view")]
    box.clear()

    await w["svc"](w["manager"]).reject_report(t.id, "more detail")
    assert _sent(box) == [(2, "notif_status_in_progress", "view"), (3, "notif_report_rejected", "view")]
    assert box.items[1].params["note"] == "more detail"
    assert box.items[0].params == {"id": t.id}  # the customer never gets notes, reports or names


async def test_manager_creating_a_ticket_does_not_notify_themselves(world):
    w, box = world, world["box"]
    await w["svc"](w["manager"]).create_ticket(w["elevator"].id, "phone call", customer_id=w["customer"].id)
    assert _sent(box) == [(11, "notif_new_ticket", "view"), (2, "notif_status_new", "view")]


async def test_people_who_never_signed_in_are_skipped(world):
    w, box = world, world["box"]
    w["tech"].telegram_id = None
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    box.clear()
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)  # works, but the technician cannot be told
    assert _sent(box) == [(2, "notif_status_assigned", "view")]


async def test_no_outbox_means_no_notifications_and_no_errors(world):
    w = world
    t = await TicketService(w["session"], w["company"], w["customer"]).create_ticket(w["elevator"].id, "x")
    assert t.id and w["box"].items == []


async def test_customer_feedback_rules(world):
    w, box = world, world["box"]
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    with pytest.raises(InvalidInput):  # too early: nothing was reported yet
        await w["svc"](w["customer"]).submit_feedback(t.id, 5)
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    await w["svc"](w["tech"]).start_work(t.id)
    await w["svc"](w["tech"]).submit_report(t.id, "fixed")
    box.clear()

    for bad in (0, 6, "abc", None):
        with pytest.raises(InvalidInput):
            await w["svc"](w["customer"]).submit_feedback(t.id, bad)
    fb = await w["svc"](w["customer"]).submit_feedback(t.id, "4", "  good work ")
    assert (fb.rating, fb.comment) == (4, "good work")
    assert _sent(box) == [(1, "notif_feedback_received", "view"), (11, "notif_feedback_received", "view")]
    assert box.items[0].params["rating"] == 4
    with pytest.raises(InvalidInput):  # only one per ticket
        await w["svc"](w["customer"]).submit_feedback(t.id, 5)
    audit = (await w["session"].execute(select(AuditLog.event_type))).scalars().all()
    assert "feedback_submitted" in audit


async def test_feedback_comment_is_optional_and_allowed_after_close(world):
    w = world
    t = await _reported(w)
    await w["svc"](w["manager"]).approve_report(t.id)
    await w["svc"](w["manager"]).close(t.id)  # closing never needed feedback
    assert t.status is TicketStatus.CLOSED
    fb = await w["svc"](w["customer"]).submit_feedback(t.id, 5, "-")
    assert fb.comment is None


async def test_only_the_ticket_customer_can_give_feedback(world):
    w = world
    t = await _reported(w)
    other = await _user(w["session"], w["company"], Role.CUSTOMER, "+9647500000009")
    with pytest.raises(NotFound):
        await w["svc"](other).submit_feedback(t.id, 5)
    for actor in (w["manager"], w["tech"]):
        with pytest.raises(PermissionDenied):
            await w["svc"](actor).submit_feedback(t.id, 5)
    assert (await w["session"].execute(select(TicketFeedback))).first() is None


async def test_manager_requests_feedback(world):
    w, box = world, world["box"]
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    with pytest.raises(InvalidInput):  # nothing reported yet
        await w["svc"](w["manager"]).request_feedback(t.id)
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    await w["svc"](w["tech"]).start_work(t.id)
    await w["svc"](w["tech"]).submit_report(t.id, "fixed")
    box.clear()

    await w["svc"](w["manager"]).request_feedback(t.id)
    assert _sent(box) == [(2, "notif_feedback_requested", "feedback")]
    assert box.items[0].language == "ckb"
    for actor in (w["customer"], w["tech"]):
        with pytest.raises(PermissionDenied):
            await w["svc"](actor).request_feedback(t.id)

    await w["svc"](w["customer"]).submit_feedback(t.id, 3)
    with pytest.raises(InvalidInput):  # already answered
        await w["svc"](w["manager"]).request_feedback(t.id)


async def test_cannot_request_feedback_from_a_customer_who_never_signed_in(world):
    w = world
    t = await _reported(w)
    w["customer"].telegram_id = None
    with pytest.raises(InvalidInput) as err:
        await w["svc"](w["manager"]).request_feedback(t.id)
    assert err.value.key == "err_customer_not_linked"


async def test_who_sees_feedback(world):
    w = world
    t = await _reported(w)
    await w["svc"](w["customer"]).submit_feedback(t.id, 2, "slow")
    assert (await w["svc"](w["manager"]).get_details(t.id)).feedback.rating == 2
    assert (await w["svc"](w["customer"]).get_details(t.id)).feedback.comment == "slow"
    assert (await w["svc"](w["tech"]).get_details(t.id)).feedback is None


async def test_customer_follows_closing_and_cancelling(world):
    w, box = world, world["box"]
    t = await _reported(w)
    box.clear()
    await w["svc"](w["manager"]).approve_report(t.id)
    assert box.items == []  # approval is internal
    await w["svc"](w["manager"]).close(t.id)
    assert _sent(box) == [(2, "notif_status_closed", "view")]
    box.clear()
    t2 = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "second")
    box.clear()
    await w["svc"](w["manager"]).cancel(t2.id)
    assert _sent(box) == [(2, "notif_status_cancelled", "view")]


async def test_changing_the_technician_without_a_status_change_is_silent_for_the_customer(world):
    w, box = world, world["box"]
    tech2 = await _user(w["session"], w["company"], Role.TECHNICIAN, "+9647500000005", telegram_id=5)
    t = await w["svc"](w["customer"]).create_ticket(w["elevator"].id, "x")
    await w["svc"](w["manager"]).assign(t.id, w["tech"].id)
    box.clear()
    await w["svc"](w["manager"]).assign(t.id, tech2.id)  # assigned -> assigned
    assert _sent(box) == [(5, "notif_assigned", "view")]
