"""Invoices: amounts, validation, numbering, delivery, cancel/resend, visibility, default currency."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.i18n import t
from app.core.outbox import Outbox
from app.core.timeutil import local_today
from app.models import AuditLog, Building, Company, Currency, Invoice, InvoiceItem, InvoiceStatus, Role, User
from app.repositories.invoices import InvoiceRepository
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.invoice_math import compute_totals
from app.services.invoices import MAX_ITEMS, InvoiceService
from app.services.validation import validate_field

YEAR = local_today().year


# ---- amounts and field validation ----


def test_totals_with_discount_and_tax():
    totals = compute_totals([(2, 50000), (1, 25000)], ("amount", 5000), Decimal("5"))
    assert (totals.subtotal, totals.discount, totals.tax_amount, totals.total) == (125000, 5000, 6000, 126000)


def test_percent_discount_and_rounding_half_up():
    # 10% of 1010 = 101; taxable 909; 5% of 909 = 45.45 -> 45
    t1 = compute_totals([(1, 1010)], ("percent", Decimal("10")), Decimal("5"))
    assert (t1.discount, t1.tax_amount, t1.total) == (101, 45, 954)
    # 5% of 1010 = 50.5 -> 51 (half up)
    assert compute_totals([(1, 1010)], None, Decimal("5")).tax_amount == 51
    # no discount and no tax
    plain = compute_totals([(3, 100)])
    assert (plain.subtotal, plain.discount, plain.tax_amount, plain.total) == (300, 0, 0, 300)


def test_discount_must_stay_below_the_subtotal():
    for discount in (("amount", 300), ("amount", 999), ("percent", Decimal("100"))):
        with pytest.raises(InvalidInput) as exc:
            compute_totals([(3, 100)], discount)
        assert exc.value.key == "err_invoice_total"
    assert compute_totals([(3, 100)], ("amount", 299)).total == 1


def test_number_validation_accepts_separators_and_other_digits():
    assert validate_field("item_price", "1,500,000") == 1500000
    assert validate_field("item_price", "۱٬۲۰۰") == 1200
    assert validate_field("item_qty", "٣") == 3
    for bad in ("0", "-5", "1.5", "abc", "", "1" * 13):
        with pytest.raises(InvalidInput):
            validate_field("item_price", bad)
    with pytest.raises(InvalidInput):
        validate_field("item_qty", "1000000")  # more than 6 digits


def test_percent_and_discount_validation():
    assert validate_field("tax", "5") == Decimal("5") and validate_field("tax", "5.25") == Decimal("5.25")
    assert validate_field("tax", "5,5") == Decimal("5.5") and validate_field("tax", "-") is None
    for bad in ("101", "-1", "5.555", "abc", "5%"):
        with pytest.raises(InvalidInput) as exc:
            validate_field("tax", bad)
        assert exc.value.key == "err_invalid_percent"
    assert validate_field("discount", "10%") == ("percent", Decimal("10"))
    assert validate_field("discount", "50 000") == ("amount", 50000)
    assert validate_field("discount", "-") is None
    for bad in ("0%", "100%", "150%", "0", "x", "10.5"):
        with pytest.raises(InvalidInput) as exc:
            validate_field("discount", bad)
        assert exc.value.key in ("err_invalid_discount", "err_invalid_percent"), bad


# ---- service ----


@pytest.fixture
async def world(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()

    def user(phone, role, tg=None, name=None, lang="en"):
        return User(company_id=company.id, phone=phone, name=name or f"U{phone[-2:]}", role=role,
                    telegram_id=tg, language=lang)

    boss = user("+9647500000001", Role.MANAGER, 101, "Boss")
    boss2 = user("+9647500000002", Role.MANAGER, 102, "Boss2")
    owner = user("+9647500000003", Role.CUSTOMER, 103, "Owner", "fa")
    stranger = user("+9647500000004", Role.CUSTOMER, 104, "Stranger")
    tech = user("+9647500000005", Role.TECHNICIAN, 105)
    session.add_all([boss, boss2, owner, stranger, tech])
    await session.flush()
    b1 = Building(company_id=company.id, owner_id=owner.id, name="Tower A", address="x")
    b2 = Building(company_id=company.id, owner_id=stranger.id, name="Tower B", address="y")
    session.add_all([b1, b2])
    await session.flush()
    return company, boss, boss2, owner, stranger, tech, b1, b2


def _svc(session, company, actor, box=None):
    return InvoiceService(session, company, actor, box)


ITEMS = [("Oil change", "2", "50,000"), ("Filter", "1", "25000")]


async def _issue(session, company, boss, owner, building, box=None, **kw):
    kw.setdefault("items", ITEMS)
    return await _svc(session, company, boss, box).issue(owner.id, building.id, "service", "IQD", **kw)


async def test_issue_stores_the_invoice_and_sends_it_to_the_customer(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    box = Outbox()
    result = await _issue(session, company, boss, owner, b1, box, discount_raw="5000", tax_raw="5", notes_raw="thanks")
    inv = result.invoice

    assert result.queued and inv.status is InvoiceStatus.ISSUED
    assert inv.number == f"{YEAR}-0001" and (inv.year, inv.seq) == (YEAR, 1) and inv.issue_date == local_today()
    assert (inv.subtotal, inv.discount, inv.tax_percent, inv.tax_amount, inv.total) == (125000, 5000, Decimal("5"), 6000, 126000)
    assert inv.currency is Currency.IQD and inv.notes == "thanks" and inv.sent_at is not None
    items = (await session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == inv.id).order_by(InvoiceItem.position))).scalars().all()
    assert [(i.position, i.description, i.quantity, i.unit_price) for i in items] == [(1, "Oil change", 2, 50000), (2, "Filter", 1, 25000)]

    [n] = box.items
    assert (n.telegram_id, n.language, n.key, n.invoice_id) == (103, "fa", "notif_invoice", inv.id)
    body = n.params["body"]  # in the customer's language (Persian)
    assert inv.number in body and "Tower A" in body and "Owner" in body and "Oil change" in body
    assert "126,000" in body and t("fa", "cur_IQD") in body and t("fa", "kind_service") in body
    audit = (await session.execute(select(AuditLog).where(AuditLog.event_type == "invoice_issued"))).scalar_one()
    assert audit.entity_id == inv.id and audit.details["number"] == inv.number and audit.actor_id == boss.id


async def test_numbers_count_up_per_year(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    a = await _issue(session, company, boss, owner, b1)
    b = await _issue(session, company, boss, owner, b1)
    assert (a.invoice.number, b.invoice.number) == (f"{YEAR}-0001", f"{YEAR}-0002")
    repo = InvoiceRepository(session)
    assert await repo.next_seq(company.id, YEAR - 1) == 1  # another year starts at 1
    assert await repo.next_seq(company.id, YEAR) == 3
    assert await repo.next_seq(company.id, YEAR - 1) == 2


async def test_customer_who_never_signed_in_is_told_to_the_manager_not_lost(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    owner.telegram_id = None
    await session.flush()
    box = Outbox()
    result = await _issue(session, company, boss, owner, b1, box)
    assert result.queued is False and box.items == [] and result.invoice.sent_at is None
    owner.telegram_id = 103
    await session.flush()
    assert await _svc(session, company, boss, box).resend(result.invoice.id) is True
    assert box.items[0].key == "notif_invoice" and result.invoice.sent_at is not None


async def test_issue_validation(session, world):
    company, boss, _, owner, stranger, tech, b1, b2 = world
    svc = _svc(session, company, boss)
    with pytest.raises(NotFound):  # the building belongs to someone else
        await svc.issue(owner.id, b2.id, "service", "IQD", ITEMS)
    with pytest.raises(NotFound):  # not a customer
        await svc.issue(tech.id, b1.id, "service", "IQD", ITEMS)
    with pytest.raises(NotFound):
        await svc.issue(owner.id, 999999, "service", "IQD", ITEMS)
    for kind, cur in (("gift", "IQD"), ("service", "USD")):
        with pytest.raises(InvalidInput):
            await svc.issue(owner.id, b1.id, kind, cur, ITEMS)
    with pytest.raises(InvalidInput) as none:
        await svc.issue(owner.id, b1.id, "service", "IQD", [])
    assert none.value.key == "err_invoice_items"
    with pytest.raises(InvalidInput) as many:
        await svc.issue(owner.id, b1.id, "service", "IQD", [("x", "1", "1")] * (MAX_ITEMS + 1))
    assert many.value.key == "err_invoice_items"
    await svc.issue(owner.id, b1.id, "service", "IQD", [("x", "1", "1")] * MAX_ITEMS)  # the limit itself is fine
    with pytest.raises(InvalidInput) as big:
        await svc.issue(owner.id, b1.id, "repair", "IRR", [("x", "1", "100")], discount_raw="100")
    assert big.value.key == "err_invoice_total"
    for bad_item in (("", "1", "5"), ("x", "0", "5"), ("x", "1", "0"), ("x" * 201, "1", "5")):
        with pytest.raises(InvalidInput):
            await svc.issue(owner.id, b1.id, "repair", "IRR", [bad_item])
    # a failed attempt leaves nothing behind except the successful invoice
    assert len((await session.execute(select(Invoice))).all()) == 1


async def test_only_managers_issue_and_manage(session, world):
    company, boss, _, owner, _, tech, b1, _ = world
    inv = (await _issue(session, company, boss, owner, b1)).invoice
    customer_svc = _svc(session, company, owner)
    for action in (
        customer_svc.issue(owner.id, b1.id, "service", "IQD", ITEMS), customer_svc.resend(inv.id),
        customer_svc.cancel(inv.id), customer_svc.set_default_currency("IQD"),
    ):
        with pytest.raises(PermissionDenied):
            await action
    with pytest.raises(PermissionDenied):
        customer_svc.default_currency()
    for actor in (tech, None):
        with pytest.raises(PermissionDenied):
            _svc(session, company, actor)


async def test_cancel_and_resend_rules(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    inv = (await _issue(session, company, boss, owner, b1)).invoice
    box = Outbox()
    svc = _svc(session, company, boss, box)
    await svc.cancel(inv.id)
    assert inv.status is InvoiceStatus.CANCELLED
    [n] = box.items
    assert (n.telegram_id, n.key, n.params) == (103, "notif_invoice_cancelled", {"number": inv.number})
    for action in (svc.cancel(inv.id), svc.resend(inv.id)):
        with pytest.raises(InvalidInput) as exc:
            await action
        assert exc.value.key == "err_invoice_state"
    details = await svc.get_details(inv.id)
    assert t("en", "inv_cancelled") in svc.text("en", details)  # still readable, marked as cancelled
    nxt = (await _issue(session, company, boss, owner, b1)).invoice
    assert nxt.number == f"{YEAR}-0002"  # the cancelled number is never reused


async def test_customers_see_only_their_own_invoices(session, world):
    company, boss, _, owner, stranger, _, b1, b2 = world
    mine = (await _issue(session, company, boss, owner, b1)).invoice
    theirs = (await _issue(session, company, boss, stranger, b2)).invoice
    svc = _svc(session, company, owner)
    rows, total = await svc.list_invoices(0)
    assert total == 1 and [i.id for i, _ in rows] == [mine.id]
    assert (await svc.get_details(mine.id)).invoice.id == mine.id
    with pytest.raises(NotFound):
        await svc.get_details(theirs.id)
    manager_rows, manager_total = await _svc(session, company, boss).list_invoices(0)
    assert manager_total == 2 and [i.id for i, _ in manager_rows] == [theirs.id, mine.id]  # newest first


async def test_list_pagination(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    for _ in range(10):
        await _issue(session, company, boss, owner, b1, items=[("x", "1", "10")])
    svc = _svc(session, company, boss)
    first, total = await svc.list_invoices(0)
    second, _ = await svc.list_invoices(1)
    assert total == 10 and len(first) == 8 and len(second) == 2


async def test_text_shows_discount_and_tax_lines_only_when_used(session, world):
    company, boss, _, owner, _, _, b1, _ = world
    svc = _svc(session, company, boss)
    plain = (await _issue(session, company, boss, owner, b1)).invoice
    full = (await _issue(session, company, boss, owner, b1, discount_raw="10%", tax_raw="5")).invoice
    plain_text = svc.text("en", await svc.get_details(plain.id))
    full_text = svc.text("en", await svc.get_details(full.id))
    assert t("en", "inv_discount") not in plain_text and "Tax" not in plain_text
    assert "125,000" in plain_text and "2 × 50,000 = 100,000" in plain_text
    assert t("en", "inv_discount") + ": \u200e-12,500" in full_text
    assert t("en", "inv_tax", percent="5") in full_text and "5,625" in full_text and "118,125" in full_text


# ---- default currency ----


async def test_default_currency_is_a_company_setting_any_manager_can_change(session, world):
    company, boss, boss2, owner, _, _, _, _ = world
    assert _svc(session, company, boss).default_currency() is None
    assert await _svc(session, company, boss).set_default_currency("IRT") is Currency.IRT
    assert company.settings["default_currency"] == "IRT"
    assert _svc(session, company, boss2).default_currency() is Currency.IRT
    await _svc(session, company, boss2).set_default_currency("IQD")  # another manager changes it
    assert _svc(session, company, boss).default_currency() is Currency.IQD
    with pytest.raises(InvalidInput):
        await _svc(session, company, boss).set_default_currency("USD")
    events = (await session.execute(select(AuditLog).where(AuditLog.event_type == "invoice_default_currency_changed").order_by(AuditLog.id))).scalars().all()
    assert [e.details for e in events] == [{"from": None, "to": "IRT"}, {"from": "IRT", "to": "IQD"}]
    company.settings = {"default_currency": "XXX"}  # damaged value is ignored
    assert _svc(session, company, boss).default_currency() is None
