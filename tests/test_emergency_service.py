"""QR emergency alert: tokens, limits, notifications, ticket creation, QR management."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.outbox import Outbox
from app.core.qr import qr_png
from app.models import (
    AlertStatus, AuditLog, Building, Company, Elevator, EmergencyAlert, Role, Ticket, User,
)
from app.services.emergency import (
    ACCOUNT_LIMIT, AlertService, EmergencyAccessService, QrCodeService, new_token, payload_for,
    token_from_payload,
)
from app.services.errors import InvalidInput, NotFound, PermissionDenied


def test_tokens_are_random_and_payloads_are_checked():
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200 and all(len(t) == 22 for t in tokens)
    token = new_token()
    assert token_from_payload(payload_for(token)) == token
    for bad in (None, "", "unit_", "unit_short", "unit_" + "a" * 33, "unit_" + "a" * 15 + "!", "other_" + token, token):
        assert token_from_payload(bad) is None, bad


def test_qr_image_is_a_png():
    png = qr_png("https://t.me/TestBot?start=unit_" + new_token())
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 200


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg=None, name=None, lang="en", **kw):
        return User(company_id=company.id, phone=phone, name=name or f"U{phone[-2:]}", role=role,
                    telegram_id=tg, language=lang, **kw)

    boss = user("+9647500000001", Role.MANAGER, 101, "Boss")
    boss2 = user("+9647500000002", Role.MANAGER, 102, "Boss2", "fa")
    unlinked = user("+9647500000006", Role.MANAGER)  # never signed in
    gone = user("+9647500000007", Role.MANAGER, 107, is_active=False)
    owner = user("+9647500000003", Role.CUSTOMER, 103, "Owner Secret")
    tech = user("+9647500000005", Role.TECHNICIAN, 105)
    session.add_all([boss, boss2, unlinked, gone, owner, tech])
    await session.flush()
    building = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="12 Main St")
    session.add(building)
    await session.flush()
    lifts = [Elevator(company_id=company.id, building_id=building.id, name=f"Lift {i}") for i in range(1, 8)]
    session.add_all(lifts)
    await session.flush()
    return company, boss, boss2, owner, tech, building, lifts


async def _token(session, company, boss, elevator):
    return (await QrCodeService(session, company, boss).get_or_create(elevator.id))[2]


# ---- QR management ----


async def test_token_is_created_once_and_can_be_renewed(session, world):
    company, boss, _, _, _, building, lifts = world
    svc = QrCodeService(session, company, boss)
    _, _, first = await svc.get_or_create(lifts[0].id)
    assert len(first) == 22 and (await svc.get_or_create(lifts[0].id))[2] == first
    _, _, other = await svc.get_or_create(lifts[1].id)
    assert other != first
    _, _, renewed = await svc.regenerate(lifts[0].id)
    assert renewed != first and lifts[0].qr_token == renewed
    access = EmergencyAccessService(session, company)
    assert await access.lookup(first) is None  # the printed old code stops working
    assert (await access.lookup(renewed))[0].id == lifts[0].id
    events = (await session.execute(select(AuditLog.event_type).where(AuditLog.entity_type == "elevator").order_by(AuditLog.id))).scalars().all()
    assert events == ["qr_created", "qr_created", "qr_regenerated"]


async def test_only_managers_manage_codes(session, world):
    company, boss, _, owner, tech, _, lifts = world
    for actor in (owner, tech, None):
        with pytest.raises(PermissionDenied):
            QrCodeService(session, company, actor)
    with pytest.raises(NotFound):
        await QrCodeService(session, company, boss).get_or_create(999999)


# ---- the scan ----


async def test_scan_creates_an_alert_and_tells_active_linked_managers_only(session, world):
    company, boss, boss2, owner, _, building, lifts = world
    token = await _token(session, company, boss, lifts[0])
    box = Outbox()
    outcome = await EmergencyAccessService(session, company, box).trigger(token, 777, "ali_u", "Ali T")

    assert outcome.state == "sent" and outcome.alert.status is AlertStatus.NEW
    assert (outcome.alert.telegram_id, outcome.alert.telegram_username, outcome.alert.name) == (777, "ali_u", "Ali T")
    assert {n.telegram_id for n in box.items} == {101, 102}  # not the unlinked or inactive managers
    n = next(x for x in box.items if x.telegram_id == 102)
    assert n.language == "fa" and n.key == "notif_emergency" and n.alert_id == outcome.alert.id
    assert n.params["building"] == "Tower A" and n.params["address"] == "12 Main St"
    assert n.params["elevator"] == "Lift 1" and n.params["name"] == "Ali T" and n.params["username"] == "@ali_u"
    assert "Owner Secret" not in str(n.params) and "+9647500000003" not in str(n.params)  # no customer data
    entry = (await session.execute(select(AuditLog).where(AuditLog.event_type == "emergency_alert_created"))).scalar_one()
    assert entry.entity_id == outcome.alert.id and entry.details == {"elevator_id": lifts[0].id, "telegram_id": 777}


async def test_unknown_token_and_inactive_units(session, world):
    company, boss, _, _, _, building, lifts = world
    access = EmergencyAccessService(session, company, Outbox())
    assert await access.trigger("x" * 22, 777, None, "A") is None
    token = await _token(session, company, boss, lifts[0])
    lifts[0].is_active = False
    await session.flush()
    out = await access.trigger(token, 777, None, "A")
    assert out.state == "inactive" and out.alert is None
    lifts[0].is_active, building.is_active = True, False
    await session.flush()
    assert (await access.trigger(token, 777, None, "A")).state == "inactive"
    assert (await session.execute(select(EmergencyAlert))).first() is None


async def test_one_alert_per_elevator_in_ten_minutes(session, world):
    company, boss, _, _, _, _, lifts = world
    token = await _token(session, company, boss, lifts[0])
    box = Outbox()
    access = EmergencyAccessService(session, company, box)
    first = await access.trigger(token, 777, None, "A")
    again = await access.trigger(token, 888, None, "B")  # someone else, same elevator
    assert (first.state, again.state) == ("sent", "recent") and again.alert is None
    assert len(box.items) == 2  # only the first alert reached the two managers
    await session.execute(update(EmergencyAlert).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=11)))
    assert (await access.trigger(token, 888, None, "B")).state == "sent"


async def test_account_limit_across_elevators(session, world):
    company, boss, _, _, _, _, lifts = world
    access = EmergencyAccessService(session, company, Outbox())
    states = []
    for lift in lifts[:ACCOUNT_LIMIT + 1]:
        token = await _token(session, company, boss, lift)
        states.append((await access.trigger(token, 777, None, "A")).state)
    assert states == ["sent"] * ACCOUNT_LIMIT + ["limited"]
    other_token = await _token(session, company, boss, lifts[ACCOUNT_LIMIT + 1])
    assert (await access.trigger(other_token, 888, None, "B")).state == "sent"  # another account is fine
    await session.execute(update(EmergencyAlert).values(created_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    assert (await access.trigger(await _token(session, company, boss, lifts[0]), 777, None, "A")).state == "sent"


# ---- handling alerts ----


async def _alert(session, company, boss, lift, tg=777):
    token = await _token(session, company, boss, lift)
    return (await EmergencyAccessService(session, company, Outbox()).trigger(token, tg, "u", "Ali")).alert


async def test_manager_turns_an_alert_into_a_ticket_for_the_owner(session, world):
    company, boss, boss2, owner, _, building, lifts = world
    alert = await _alert(session, company, boss, lifts[0])
    box = Outbox()
    ticket = await AlertService(session, company, boss2, box).create_ticket(alert.id)

    assert alert.status is AlertStatus.TICKET_CREATED and alert.ticket_id == ticket.id
    assert alert.handled_by_id == boss2.id and alert.handled_at is not None
    stored = (await session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert stored.customer_id == owner.id and stored.elevator_id == lifts[0].id
    assert f"#{alert.id}" in stored.description and "Lift 1" in stored.description
    assert 101 in {n.telegram_id for n in box.items}  # the other manager hears about the new ticket
    with pytest.raises(InvalidInput) as again:
        await AlertService(session, company, boss, Outbox()).create_ticket(alert.id)
    assert again.value.key == "err_alert_state"
    with pytest.raises(InvalidInput):
        await AlertService(session, company, boss, Outbox()).dismiss(alert.id)


async def test_ticket_is_refused_for_an_inactive_building_and_the_alert_stays_open(session, world):
    company, boss, _, _, _, building, lifts = world
    alert = await _alert(session, company, boss, lifts[0])
    building.is_active = False
    await session.flush()
    with pytest.raises(InvalidInput) as exc:
        await AlertService(session, company, boss, Outbox()).create_ticket(alert.id)
    assert exc.value.key == "err_inactive_parent" and alert.status is AlertStatus.NEW


async def test_dismiss_and_lists(session, world):
    company, boss, _, _, _, _, lifts = world
    a1 = await _alert(session, company, boss, lifts[0], 701)
    a2 = await _alert(session, company, boss, lifts[1], 702)
    a3 = await _alert(session, company, boss, lifts[2], 703)
    svc = AlertService(session, company, boss, Outbox())
    await svc.dismiss(a1.id)
    assert a1.status is AlertStatus.DISMISSED and a1.handled_by_id == boss.id
    rows, total, new = await svc.list_alerts(0)
    assert (total, new) == (3, 2) and [a.id for a, _, _ in rows] == [a3.id, a2.id, a1.id]  # new first, newest first
    details = await svc.get_details(a1.id)
    assert details.owner.name == "Owner Secret" and details.handler.id == boss.id  # managers may see the owner
    with pytest.raises(InvalidInput):
        await svc.dismiss(a1.id)
    with pytest.raises(NotFound):
        await svc.get_details(999999)
    for event in ("emergency_alert_dismissed",):
        assert (await session.execute(select(AuditLog).where(AuditLog.event_type == event))).scalar_one()


async def test_only_managers_handle_alerts(session, world):
    company, _, _, owner, tech, _, _ = world
    for actor in (owner, tech, None):
        with pytest.raises(PermissionDenied):
            AlertService(session, company, actor)
