"""Ticket screens end to end for customer, manager and technician (fake Telegram)."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.models import (
    Building, Company, Elevator, ReviewStatus, Role, Ticket, TicketAttachment, TicketReport,
    TicketStatus, User,
)
from bot_helpers import (
    TG_ID, FakeSession, Inject, button_update, photo_update, text_update,
)

CUSTOMER_TG, TECH_TG = 555, 666


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    async def add(role, phone, tg, **kw):
        user = User(company_id=company.id, phone=phone, name=kw.pop("name", role.value), role=role,
                    telegram_id=tg, language="en")
        session.add(user)
        await session.flush()
        return user

    manager = await add(Role.MANAGER, "+9647500000001", TG_ID)
    customer = await add(Role.CUSTOMER, "+9647500000002", CUSTOMER_TG, name="Ali")
    tech = await add(Role.TECHNICIAN, "+9647500000003", TECH_TG, name="Tek")
    building = Building(company_id=company.id, owner_id=customer.id, name="Tower", address="Erbil")
    session.add(building)
    await session.flush()
    elevator = Elevator(company_id=company.id, building_id=building.id, name="E1")
    session.add(elevator)
    await session.flush()

    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 10_000))

    async def send(factory, *args, tg_id=TG_ID):
        await dp.feed_update(bot, factory(*args, next(counter), tg_id=tg_id))

    return dict(session=session, company=company, manager=manager, customer=customer, tech=tech,
                elevator=elevator, fake=fake, send=send)


async def _ticket(session):
    return (await session.execute(select(Ticket))).scalar_one()


async def _report_a_breakdown(w):
    send = w["send"]
    await send(button_update, "t:new", tg_id=CUSTOMER_TG)
    await send(button_update, f"t:pick:{w['elevator'].id}", tg_id=CUSTOMER_TG)
    await send(text_update, "Door stuck on floor 3", tg_id=CUSTOMER_TG)
    await send(photo_update, "PHOTO1", tg_id=CUSTOMER_TG)
    await send(button_update, "t:confirm", tg_id=CUSTOMER_TG)
    return await _ticket(w["session"])


async def test_customer_reports_with_photo(world):
    w = world
    send, fake = w["send"], w["fake"]
    await send(button_update, "t:new", tg_id=CUSTOMER_TG)
    assert fake.sent[-1] == t("en", "prompt_pick_elevator")
    await send(button_update, f"t:pick:{w['elevator'].id}", tg_id=CUSTOMER_TG)
    assert fake.sent[-1].startswith(t("en", "prompt_description"))
    await send(text_update, "Door stuck on floor 3", tg_id=CUSTOMER_TG)
    assert fake.sent[-1].startswith(t("en", "prompt_photo"))
    await send(photo_update, "PHOTO1", tg_id=CUSTOMER_TG)
    assert fake.sent[-1].startswith(t("en", "summary_title"))
    await send(button_update, "t:confirm", tg_id=CUSTOMER_TG)
    assert fake.last_to(CUSTOMER_TG).startswith(t("en", "created"))

    ticket = await _ticket(w["session"])
    assert ticket.status is TicketStatus.NEW and ticket.customer_id == w["customer"].id
    files = (await w["session"].execute(select(TicketAttachment.telegram_file_id))).scalars().all()
    assert files == ["PHOTO1"]
    assert "Erbil" not in fake.last_to(CUSTOMER_TG) and "+9647500000002" not in fake.last_to(CUSTOMER_TG)


async def test_customer_can_skip_photo_and_cancel_the_draft(world):
    w = world
    send, fake = w["send"], w["fake"]
    await send(button_update, "t:new", tg_id=CUSTOMER_TG)
    await send(button_update, f"t:pick:{w['elevator'].id}", tg_id=CUSTOMER_TG)
    await send(text_update, "Strange noise", tg_id=CUSTOMER_TG)
    await send(text_update, "-", tg_id=CUSTOMER_TG)
    await send(text_update, "/cancel", tg_id=CUSTOMER_TG)
    assert t("en", "cancelled") in fake.sent
    await send(button_update, "t:confirm", tg_id=CUSTOMER_TG)  # stale button
    assert fake.sent[-1] == t("en", "err_ticket_state")
    assert (await w["session"].execute(select(Ticket))).first() is None


async def test_customer_without_elevators_gets_a_message(world):
    w = world
    w["elevator"].is_active = False
    await w["send"](button_update, "t:new", tg_id=CUSTOMER_TG)
    assert w["fake"].sent[-1] == t("en", "no_elevators_registered")


async def test_manager_reports_on_behalf_of_customer(world):
    w = world
    send = w["send"]
    await send(button_update, f"t:new_for:{w['customer'].id}")
    await send(button_update, f"t:pick:{w['elevator'].id}")
    await send(text_update, "Reported by phone")
    await send(text_update, "-")
    await send(button_update, "t:confirm")
    ticket = await _ticket(w["session"])
    assert ticket.customer_id == w["customer"].id and ticket.description == "Reported by phone"


async def test_full_lifecycle_through_the_bot(world):
    w = world
    send, fake, session = w["send"], w["fake"], w["session"]
    ticket = await _report_a_breakdown(w)

    # manager sees it, assigns it
    await send(button_update, "t:groups")
    assert fake.sent[-1] == t("en", "btn_tickets")
    await send(button_update, "t:list:new:0")
    assert fake.sent[-1].startswith(t("en", "grp_new") + " (1)")
    await send(button_update, f"t:view:{ticket.id}")
    assert "Door stuck on floor 3" in fake.sent[-1] and "+9647500000002" in fake.sent[-1]
    await send(button_update, f"t:photos:{ticket.id}")
    assert fake.sent[-1] == f"#{ticket.id}"  # the photo caption
    await send(button_update, f"t:assign:{ticket.id}")
    assert fake.sent[-1] == t("en", "prompt_pick_technician")
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    assert ticket.status is TicketStatus.ASSIGNED

    # technician starts and reports
    await send(button_update, "t:list:tasks:0", tg_id=TECH_TG)
    assert fake.sent[-1].startswith(t("en", "title_my_tasks") + " (1)")
    await send(button_update, f"t:start:{ticket.id}", tg_id=TECH_TG)
    assert ticket.status is TicketStatus.IN_PROGRESS
    await send(button_update, f"t:report:{ticket.id}", tg_id=TECH_TG)
    assert fake.sent[-1].startswith(t("en", "prompt_report"))
    await send(text_update, "Replaced the door sensor", tg_id=TECH_TG)
    assert fake.last_to(TECH_TG).startswith(t("en", "msg_report_sent"))
    assert ticket.status is TicketStatus.REPORTED

    # the customer never sees the technician's report
    await send(button_update, f"t:view:{ticket.id}", tg_id=CUSTOMER_TG)
    assert "Replaced the door sensor" not in fake.sent[-1]

    # manager rejects with a note, technician reports again
    await send(button_update, f"t:reject:{ticket.id}")
    assert fake.sent[-1].startswith(t("en", "prompt_note"))
    await send(text_update, "Please add a photo of the repair")
    assert ticket.status is TicketStatus.IN_PROGRESS
    await send(button_update, f"t:view:{ticket.id}", tg_id=TECH_TG)
    assert "Please add a photo of the repair" in fake.sent[-1]  # technician sees the note
    await send(button_update, f"t:report:{ticket.id}", tg_id=TECH_TG)
    await send(text_update, "Second report with details", tg_id=TECH_TG)

    # manager must approve before closing
    await send(button_update, f"t:close:{ticket.id}")
    assert fake.sent[-1] == t("en", "err_report_not_approved")
    await send(button_update, f"t:approve:{ticket.id}")
    reports = (await session.execute(select(TicketReport).order_by(TicketReport.id))).scalars().all()
    assert [r.review_status for r in reports] == [ReviewStatus.REJECTED, ReviewStatus.APPROVED]
    await send(button_update, f"t:close:{ticket.id}")
    assert ticket.status is TicketStatus.CLOSED
    await send(button_update, "t:list:done:0")
    assert fake.sent[-1].startswith(t("en", "grp_done") + " (1)")


async def test_manager_cancels_a_ticket_with_confirmation(world):
    w = world
    ticket = await _report_a_breakdown(w)
    await w["send"](button_update, f"t:cancel:{ticket.id}")
    assert w["fake"].sent[-1] == t("en", "confirm_cancel_ticket", id=ticket.id)
    assert ticket.status is TicketStatus.NEW  # nothing changes until confirmed
    await w["send"](button_update, f"t:cancel_ok:{ticket.id}")
    assert ticket.status is TicketStatus.CANCELLED


async def test_roles_and_ownership_are_enforced_on_buttons(world):
    w = world
    send, fake = w["send"], w["fake"]
    ticket = await _report_a_breakdown(w)
    await send(button_update, f"t:assign:{ticket.id}", tg_id=CUSTOMER_TG)
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}", tg_id=TECH_TG)
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"t:cancel_ok:{ticket.id}", tg_id=CUSTOMER_TG)
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"t:view:{ticket.id}", tg_id=TECH_TG)  # not assigned to this technician
    assert fake.sent[-1] == t("en", "err_not_found")
    await send(button_update, "t:list:new:0", tg_id=CUSTOMER_TG)
    assert fake.sent[-1] == t("en", "no_permission")
    assert ticket.status is TicketStatus.NEW and ticket.technician_id is None


async def test_notifications_reach_the_right_people(world):
    w = world
    send, fake, session = w["send"], w["fake"], w["session"]

    def got(chat, prefix):
        return [text for c, text in fake.sent_to if c == chat and text.startswith(prefix)]

    ticket = await _report_a_breakdown(w)
    assert len(got(TG_ID, "🆕 New ticket")) == 1  # the manager is told about the new ticket
    assert "Ali — Tower / E1" in got(TG_ID, "🆕")[0]
    assert not got(CUSTOMER_TG, "🆕")

    await send(button_update, f"t:assign:{ticket.id}")
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    assert len(got(TECH_TG, "👷 New task")) == 1  # the technician is told about the assignment

    await send(button_update, f"t:start:{ticket.id}", tg_id=TECH_TG)
    await send(button_update, f"t:report:{ticket.id}", tg_id=TECH_TG)
    await send(text_update, "Replaced the sensor", tg_id=TECH_TG)
    assert len(got(TG_ID, "📝 Ticket")) == 1  # the manager is told a report is waiting

    await send(button_update, f"t:reject:{ticket.id}")
    await send(text_update, "Add a photo please")
    assert "Add a photo please" in got(TECH_TG, "↩️")[0]  # the technician gets the note

    await send(button_update, f"t:report:{ticket.id}", tg_id=TECH_TG)
    await send(text_update, "Second report", tg_id=TECH_TG)
    await send(button_update, f"t:approve:{ticket.id}")

    # manager asks for feedback; the customer is notified and answers
    await send(button_update, f"t:askfb:{ticket.id}")
    assert len(got(CUSTOMER_TG, "⭐ Ticket")) == 1
    await send(button_update, f"t:feedback:{ticket.id}", tg_id=CUSTOMER_TG)
    assert fake.last_to(CUSTOMER_TG) == t("en", "prompt_rating")
    await send(button_update, f"t:rate:{ticket.id}:4", tg_id=CUSTOMER_TG)
    assert fake.last_to(CUSTOMER_TG).startswith(t("en", "prompt_comment"))
    await send(text_update, "Fast and polite", tg_id=CUSTOMER_TG)
    assert fake.last_to(CUSTOMER_TG).startswith(t("en", "msg_feedback_sent"))
    assert "⭐⭐⭐⭐ (4/5)" in fake.last_to(CUSTOMER_TG) and "Fast and polite" in fake.last_to(CUSTOMER_TG)
    assert "rated the service 4/5" in got(TG_ID, "⭐ Ticket")[-1]  # the manager sees it arrive

    # the manager sees the feedback on the card and can no longer request it
    await send(button_update, f"t:view:{ticket.id}")
    assert "Fast and polite" in fake.last_to(TG_ID)
    await send(button_update, f"t:askfb:{ticket.id}")
    assert fake.sent[-1] == t("en", "err_feedback_exists")  # alerts carry no chat id

    # closing never needed the feedback, but it is there for the decision
    await send(button_update, f"t:close:{ticket.id}")
    assert ticket.status is TicketStatus.CLOSED


async def test_failed_actions_send_no_notifications(world):
    w = world
    send, fake = w["send"], w["fake"]
    ticket = await _report_a_breakdown(w)
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    before = len(fake.sent_to)
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")  # same technician again
    assert fake.sent[-1] == t("en", "err_same_technician")
    assert not [1 for chat, _ in fake.sent_to[before:] if chat == TECH_TG]  # the technician is not told again


async def test_blocked_recipient_does_not_break_the_action(world):
    w = world
    send, fake = w["send"], w["fake"]
    ticket = await _report_a_breakdown(w)
    fake.blocked.add(TECH_TG)  # the technician blocked the bot
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    assert ticket.status is TicketStatus.ASSIGNED
    assert fake.last_to(TG_ID).startswith("🎫")  # the card was still shown


async def test_unlinked_technician_triggers_a_warning(world):
    w = world
    ticket = await _report_a_breakdown(w)
    w["tech"].telegram_id = None
    await w["send"](button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    assert t("en", "msg_technician_not_linked") in w["fake"].sent
    assert ticket.status is TicketStatus.ASSIGNED


async def test_feedback_buttons_are_for_the_ticket_customer_only(world):
    w = world
    send, fake = w["send"], w["fake"]
    ticket = await _report_a_breakdown(w)
    await send(button_update, f"t:feedback:{ticket.id}", tg_id=CUSTOMER_TG)  # nothing reported yet
    assert fake.sent[-1] == t("en", "err_ticket_state")
    await send(button_update, f"t:feedback:{ticket.id}")  # a manager cannot give feedback
    assert fake.sent[-1] == t("en", "no_permission")
    await send(button_update, f"t:askfb:{ticket.id}", tg_id=CUSTOMER_TG)
    assert fake.sent[-1] == t("en", "no_permission")


async def test_customer_follows_the_progress_of_their_ticket(world):
    w = world
    send, fake = w["send"], w["fake"]

    def got(chat, prefix):
        return [text for c, text in fake.sent_to if c == chat and text.startswith(prefix)]

    ticket = await _report_a_breakdown(w)
    await send(button_update, f"t:set:{ticket.id}:{w['tech'].id}")
    assert got(CUSTOMER_TG, "👷 Ticket") == [f"👷 Ticket #{ticket.id}: a technician has been assigned."]
    await send(button_update, f"t:start:{ticket.id}", tg_id=TECH_TG)
    assert got(CUSTOMER_TG, "🔧 Ticket") == [f"🔧 Ticket #{ticket.id}: work is in progress."]
    await send(button_update, f"t:report:{ticket.id}", tg_id=TECH_TG)
    await send(text_update, "Replaced the sensor secretly", tg_id=TECH_TG)
    assert len(got(CUSTOMER_TG, "📝 Ticket")) == 1
    await send(button_update, f"t:approve:{ticket.id}")
    await send(button_update, f"t:close:{ticket.id}")
    assert got(CUSTOMER_TG, "✅ Ticket") == [f"✅ Ticket #{ticket.id} is closed."]

    everything_the_customer_got = " ".join(text for c, text in fake.sent_to if c == CUSTOMER_TG)
    assert "secretly" not in everything_the_customer_got  # no report content
    assert "Tek" not in everything_the_customer_got  # no technician identity
