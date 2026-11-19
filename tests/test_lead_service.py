"""Guest consultation requests: validation, daily limit, notifications, manager handling."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.outbox import Outbox
from app.models import AuditLog, Company, Lead, LeadStatus, Role, User
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from app.services.leads import MAX_LEADS_PER_DAY, GuestLeadService, LeadService


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def make(phone, role, tg=None, active=True, language="en"):
        return User(company_id=company.id, phone=phone, name=f"U{phone[-2:]}", role=role,
                    telegram_id=tg, language=language, is_active=active)

    boss = make("+9647500000001", Role.MANAGER, tg=101)
    second = make("+9647500000002", Role.MANAGER, tg=102, language="fa")
    unlinked = make("+9647500000003", Role.MANAGER)  # never signed in
    gone = make("+9647500000004", Role.MANAGER, tg=104, active=False)
    customer = make("+9647500000005", Role.CUSTOMER, tg=105)
    session.add_all([boss, second, unlinked, gone, customer])
    await session.flush()
    return company, boss, second, customer


async def _submit(session, company, box=None, tg=555, phone="07501234567", text="I need a new elevator"):
    return await GuestLeadService(session, company, box).submit(tg, "ali_u", "Ali T", phone, text)


async def test_submit_stores_lead_and_notifies_only_active_linked_managers(session, world):
    company, boss, second, _ = world
    box = Outbox()
    lead = await _submit(session, company, box)

    assert lead.status is LeadStatus.NEW and lead.phone == "+9647501234567"
    assert lead.telegram_id == 555 and lead.telegram_username == "ali_u"
    sent = {n.telegram_id: n for n in box.items}
    assert set(sent) == {101, 102}  # not the unlinked, inactive or customer accounts
    assert sent[102].language == "fa" and sent[101].language == "en"
    assert sent[101].key == "notif_new_lead" and sent[101].lead_id == lead.id
    assert sent[101].params["phone"] == "+9647501234567" and sent[101].params["username"] == "@ali_u"


async def test_invalid_phone_and_message_are_rejected(session, world):
    company, *_ = world
    with pytest.raises(InvalidInput) as bad_phone:
        await _submit(session, company, phone="12")
    assert bad_phone.value.key == "invalid_phone"
    with pytest.raises(InvalidInput):
        await _submit(session, company, text="   ")
    with pytest.raises(InvalidInput) as too_long:
        await _submit(session, company, text="x" * 1001)
    assert too_long.value.key == "err_text_too_long"
    assert (await session.execute(select(Lead))).first() is None


async def test_daily_limit_per_telegram_account(session, world):
    company, *_ = world
    for _ in range(MAX_LEADS_PER_DAY):
        await _submit(session, company)
    with pytest.raises(Conflict) as exc:
        await _submit(session, company)
    assert exc.value.key == "err_lead_limit" and exc.value.params == {"max": MAX_LEADS_PER_DAY}
    with pytest.raises(Conflict):  # the early check used by the bot says the same
        await GuestLeadService(session, company).check_allowed(555)
    await _submit(session, company, tg=556)  # another account is not affected


async def test_old_requests_do_not_count_towards_the_limit(session, world):
    company, *_ = world
    for _ in range(MAX_LEADS_PER_DAY):
        await _submit(session, company)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.execute(update(Lead).values(created_at=old))
    await _submit(session, company)  # allowed again


async def test_audit_log_keeps_only_last_four_digits(session, world):
    company, *_ = world
    lead = await _submit(session, company)
    entry = (await session.execute(
        select(AuditLog).where(AuditLog.event_type == "lead_created")
    )).scalar_one()
    assert entry.entity_id == lead.id and entry.actor_id is None
    assert entry.details == {"phone_last4": "4567"}


async def test_only_managers_can_work_with_leads(session, world):
    company, boss, _, customer = world
    for actor in (customer, None):
        with pytest.raises(PermissionDenied):
            LeadService(session, company, actor)
    LeadService(session, company, boss)  # fine


async def test_manager_lists_and_handles_requests(session, world):
    company, boss, second, _ = world
    a = await _submit(session, company, tg=1)
    b = await _submit(session, company, tg=2)
    c = await _submit(session, company, tg=3)
    svc = LeadService(session, company, boss)

    await svc.set_status(a.id, LeadStatus.CONTACTED)
    await svc.set_status(c.id, LeadStatus.CLOSED)
    rows, total, new = await svc.list_leads(0)
    assert total == 3 and new == 1
    assert [x.id for x in rows] == [b.id, a.id, c.id]  # new, followed up, closed

    lead, handler = await svc.get(a.id)
    assert lead.handled_by_id == boss.id and lead.handled_at is not None and handler.id == boss.id
    _, none_yet = await svc.get(b.id)
    assert none_yet is None


async def test_status_moves_are_checked_and_audited(session, world):
    company, boss, *_ = world
    lead = await _submit(session, company)
    svc = LeadService(session, company, boss)
    await svc.set_status(lead.id, LeadStatus.CONTACTED)
    with pytest.raises(InvalidInput) as exc:
        await svc.set_status(lead.id, LeadStatus.CONTACTED)
    assert exc.value.key == "err_lead_state"
    await svc.set_status(lead.id, LeadStatus.CLOSED)
    with pytest.raises(InvalidInput):  # closed is final
        await svc.set_status(lead.id, LeadStatus.CONTACTED)
    with pytest.raises(NotFound):
        await svc.set_status(999999, LeadStatus.CLOSED)
    events = (await session.execute(
        select(AuditLog).where(AuditLog.event_type == "lead_status_changed").order_by(AuditLog.id)
    )).scalars().all()
    assert [e.details for e in events] == [
        {"from": "new", "to": "contacted"}, {"from": "contacted", "to": "closed"},
    ]


async def test_pagination(session, world):
    company, boss, *_ = world
    for i in range(10):
        await _submit(session, company, tg=1000 + i)
    svc = LeadService(session, company, boss)
    page0, total, _ = await svc.list_leads(0)
    page1, _, _ = await svc.list_leads(1)
    assert total == 10 and len(page0) == 8 and len(page1) == 2
