"""Invoice screens end to end with a fake Telegram session."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.core.timeutil import local_today
from app.models import Building, Company, Invoice, InvoiceStatus, Role, User
from bot_helpers import FakeSession, Inject, button_update, text_update

MANAGER, OWNER, STRANGER, TECH = 101, 103, 104, 105
YEAR = local_today().year


@pytest.fixture
async def env(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg, name):
        return User(company_id=company.id, phone=phone, name=name, role=role, telegram_id=tg, language="en")

    boss = user("+9647500000001", Role.MANAGER, MANAGER, "Boss")
    owner = user("+9647500000003", Role.CUSTOMER, OWNER, "Owner")
    stranger = user("+9647500000004", Role.CUSTOMER, STRANGER, "Stranger")
    tech = user("+9647500000005", Role.TECHNICIAN, TECH, "Tech")
    session.add_all([boss, owner, stranger, tech])
    await session.flush()
    mine = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    theirs = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([mine, theirs])
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 2000))

    async def send(factory, *args, **kw):
        kw.setdefault("tg_id", MANAGER)
        await dp.feed_update(bot, factory(*args, next(counter), **kw))

    return company, session, fake, send, owner, stranger, mine, theirs


async def _invoices(session):
    return list((await session.execute(select(Invoice).order_by(Invoice.id))).scalars())


async def _start(send, building, kind="service"):
    await send(button_update, f"i:b:{building.id}")
    await send(button_update, f"i:k:{kind}")


async def _add_item(send, desc, qty, price):
    await send(button_update, "i:add")
    await send(text_update, desc)
    await send(text_update, qty)
    await send(text_update, price)


async def test_full_flow_from_the_menu_with_discount_tax_and_notes(env):
    company, session, fake, send, owner, _, mine, _ = env
    await send(button_update, "i:menu")
    assert fake.sent[-1] == t("en", "btn_invoices")
    await send(button_update, "i:new")
    assert fake.sent[-1].startswith(t("en", "pick_customer"))
    await send(button_update, f"i:pb:{owner.id}")
    assert fake.sent[-1].startswith(t("en", "pick_building"))
    await send(button_update, f"i:b:{mine.id}")
    assert fake.sent[-1] == t("en", "pick_kind")
    await send(button_update, "i:k:repair")
    assert fake.sent[-1] == t("en", "pick_default_currency")  # the very first invoice: ask once
    await send(button_update, "i:dcf:IRT")
    draft = fake.sent[-1]
    assert t("en", "inv_title_draft") in draft and t("en", "cur_IRT") in draft and t("en", "inv_no_items") in draft
    assert company.settings["default_currency"] == "IRT"

    await _add_item(send, "Door motor", "2", "1,500,000")
    assert "Door motor" in fake.sent[-1] and "2 × 1,500,000 = 3,000,000" in fake.sent[-1]
    await _add_item(send, "Labour", "1", "200000")
    await send(button_update, "i:disc")
    assert fake.sent[-1].startswith(t("en", "prompt_discount"))
    await send(text_update, "10%")
    await send(button_update, "i:tax")
    await send(text_update, "5")
    await send(button_update, "i:notes")
    await send(text_update, "Paid within 7 days")
    preview = fake.sent[-1]
    assert "3,200,000" in preview and "-320,000" in preview and "144,000" in preview and "3,024,000" in preview
    assert await _invoices(session) == []  # nothing is stored before "Issue"

    await send(button_update, "i:issue")
    [inv] = await _invoices(session)
    assert inv.number == f"{YEAR}-0001" and inv.total == 3024000 and inv.status is InvoiceStatus.ISSUED
    assert inv.customer_id == owner.id and inv.building_id == mine.id and inv.currency.value == "IRT"
    assert t("en", "msg_invoice_issued") in fake.sent
    assert t("en", "inv_title", number=inv.number) in fake.last_to(MANAGER)
    delivered = fake.last_to(OWNER)
    assert inv.number in delivered and "Door motor" in delivered and "3,024,000" in delivered and "Paid within 7 days" in delivered


async def test_entry_from_the_building_card_skips_the_pickers_and_uses_the_default_currency(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine, "renewal")
    assert t("en", "cur_IQD") in fake.sent[-1] and t("en", "kind_renewal") in fake.sent[-1]
    assert "Owner" in fake.sent[-1] and "Tower A" in fake.sent[-1]
    await _add_item(send, "Contract renewal", "1", "900000")
    await send(button_update, "i:issue")
    [inv] = await _invoices(session)
    assert inv.currency.value == "IQD" and inv.kind.value == "renewal" and (inv.discount, inv.tax_amount) == (0, 0)


async def test_currency_can_be_changed_per_invoice_without_touching_the_default(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await send(button_update, "i:cur")
    assert fake.sent[-1] == t("en", "pick_currency")
    await send(button_update, "i:setcur:IRR")
    assert t("en", "cur_IRR") in fake.sent[-1]
    await _add_item(send, "x", "1", "10")
    await send(button_update, "i:issue")
    assert (await _invoices(session))[0].currency.value == "IRR"
    assert company.settings["default_currency"] == "IQD"
    await send(button_update, "i:setcur:EUR")  # no draft any more -> refused politely
    assert fake.sent[-1] == t("en", "err_draft_lost")


async def test_manager_changes_the_default_currency_from_the_menu(env):
    company, _, fake, send, *_ = env
    await send(button_update, "i:menu")
    await send(button_update, "i:dcs")
    assert fake.sent[-1] == t("en", "pick_default_currency")
    await send(button_update, "i:dc:IRR")
    assert company.settings["default_currency"] == "IRR"
    assert t("en", "msg_default_currency_set") in fake.sent
    await send(button_update, "i:dc:USD")
    assert fake.sent[-1] == t("en", "err_invalid_text")


async def test_bad_input_keeps_the_step_open(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await send(button_update, "i:add")
    await send(text_update, "Part")
    await send(text_update, "two")
    assert fake.sent[-1] == t("en", "err_invalid_number")
    await send(text_update, "2")
    await send(text_update, "-5")
    assert fake.sent[-1] == t("en", "err_invalid_number")
    await send(text_update, "100")
    assert "2 × 100 = 200" in fake.sent[-1]
    await send(button_update, "i:disc")
    await send(text_update, "500")  # bigger than the subtotal
    assert fake.sent[-1] == t("en", "err_invoice_total")
    await send(text_update, "50%")
    await send(button_update, "i:tax")
    await send(text_update, "150")
    assert fake.sent[-1] == t("en", "err_invalid_percent")
    await send(text_update, "-")
    assert "100" in fake.sent[-1] and t("en", "inv_discount") in fake.sent[-1]


async def test_removing_items_and_a_discount_that_no_longer_fits(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await _add_item(send, "Big", "1", "1000")
    await _add_item(send, "Small", "1", "100")
    await send(button_update, "i:disc")
    await send(text_update, "500")
    await send(button_update, "i:rm")  # removes "Small": still fine
    assert "Small" not in fake.sent[-1] and "Big" in fake.sent[-1]
    await send(button_update, "i:rm")
    assert t("en", "inv_no_items") in fake.sent[-1]
    await send(button_update, "i:issue")  # no items -> refused
    assert fake.sent[-1] == t("en", "err_invoice_items", max=20)
    assert await _invoices(session) == []


async def test_discard_and_cancel_clear_the_draft(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await _add_item(send, "x", "1", "10")
    await send(button_update, "i:drop")
    await send(button_update, "i:issue")
    assert fake.sent[-1] == t("en", "err_draft_lost")
    await _start(send, mine)
    await send(button_update, "i:add")
    await send(text_update, "/cancel")
    assert fake.sent[-2] == t("en", "cancelled")
    await send(button_update, "i:issue")
    assert fake.sent[-1] == t("en", "err_draft_lost")
    assert await _invoices(session) == []


async def test_manager_resends_and_cancels_with_confirmation(env):
    company, session, fake, send, owner, _, mine, _ = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await _add_item(send, "x", "1", "10")
    await send(button_update, "i:issue")
    [inv] = await _invoices(session)

    await send(button_update, "i:list:all:0")
    assert fake.sent[-1] == t("en", "title_invoices") + " (1)"
    await send(button_update, f"i:resend:{inv.id}")
    assert t("en", "msg_invoice_sent") in fake.sent
    await send(button_update, f"i:cx:{inv.id}")
    assert fake.sent[-1] == t("en", "confirm_cancel_invoice", number=inv.number)
    assert inv.status is InvoiceStatus.ISSUED  # asking changes nothing
    await send(button_update, f"i:cx_ok:{inv.id}")
    assert inv.status is InvoiceStatus.CANCELLED
    assert any(t("en", "inv_cancelled") in m for m in fake.sent)  # the card now shows the cancelled mark
    assert fake.last_to(OWNER) == t("en", "notif_invoice_cancelled", number=inv.number)
    await send(button_update, f"i:resend:{inv.id}")
    assert fake.sent[-1] == t("en", "err_invoice_state")


async def test_customer_reads_own_invoices_only(env):
    company, session, fake, send, owner, stranger, mine, theirs = env
    company.settings = {"default_currency": "IQD"}
    for building in (mine, theirs):
        await _start(send, building)
        await _add_item(send, "x", "1", "10")
        await send(button_update, "i:issue")
    own, other = await _invoices(session)

    await send(button_update, "i:list:mine:0", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "title_my_invoices") + " (1)"
    await send(button_update, f"i:view:{own.id}", tg_id=OWNER)
    assert own.number in fake.sent[-1] and "Tower A" in fake.sent[-1]
    await send(button_update, f"i:view:{other.id}", tg_id=OWNER)
    assert fake.sent[-1] == t("en", "err_not_found")


async def test_access_rules(env):
    company, session, fake, send, owner, stranger, mine, theirs = env
    company.settings = {"default_currency": "IQD"}
    await _start(send, mine)
    await _add_item(send, "x", "1", "10")
    await send(button_update, "i:issue")
    [inv] = await _invoices(session)

    manager_only = ("i:menu", "i:new", "i:pc:0", f"i:pb:{owner.id}", f"i:b:{mine.id}", "i:k:service", "i:add", "i:issue",
                    "i:dcs", "i:dc:IQD", f"i:resend:{inv.id}", f"i:cx:{inv.id}", f"i:cx_ok:{inv.id}", "i:list:all:0")
    for tg in (OWNER, STRANGER, TECH):
        for data in manager_only:
            await send(button_update, data, tg_id=tg)
            assert fake.sent[-1] == t("en", "no_permission"), (tg, data)
    assert inv.status is InvoiceStatus.ISSUED and len(await _invoices(session)) == 1


async def test_garbled_callbacks_are_rejected_politely(env):
    _, _, fake, send, *_ = env
    for data in ("i:view:abc", "i:list:all", "i:list:all:x", "i:list:mine:0", "i:pb:", "i:b:x", "i:resend:"):
        await send(button_update, data)
        assert fake.sent[-1] == t("en", "err_invalid_text"), data
