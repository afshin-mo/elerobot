"""Invoice screens (FR-15). Managers build an invoice step by step (draft kept in the FSM
data, nothing is stored until "Issue and send"); customers read their own invoices.
Authorization and all amounts are in InvoiceService."""
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import views
from app.core.i18n import t
from app.models import Company, Currency, InvoiceKind, Role
from app.services.auth import AuthService
from app.services.directory import DirectoryService
from app.services.errors import InvalidInput
from app.services.invoice_format import render
from app.services.invoices import MAX_ITEMS, InvoiceService
from app.services.validation import validate_field


class Draft(StatesGroup):
    desc = State()
    qty = State()
    price = State()
    discount = State()
    tax = State()
    notes = State()


def _int(data: str, index: int = 2) -> int:
    try:
        return int(data.split(":")[index])
    except (ValueError, IndexError):
        raise InvalidInput() from None


async def _context(session: AsyncSession, company: Company, telegram_id: int):
    actor = await AuthService(session, company, None).get_by_telegram_id(telegram_id)
    svc = InvoiceService(session, company, actor)  # raises PermissionDenied for other roles
    return svc, (actor.language or company.default_language)


async def _manager_context(session: AsyncSession, company: Company, telegram_id: int):
    svc, lang = await _context(session, company, telegram_id)
    svc.require_manager()
    return svc, lang


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # "message is not modified"
        pass


async def _draft(state: FSMContext) -> dict:
    data = await state.get_data()
    if "cust" not in data or "kind" not in data:
        raise InvalidInput("err_draft_lost")
    return data


def _draft_view(svc: InvoiceService, company: Company, lang: str, data: dict):
    """Text and buttons of the draft screen with the current totals."""
    items = data.get("items", [])
    totals, warning = None, None
    if items:
        try:
            totals = svc.draft_totals(items, data.get("discount"), data.get("tax"))
        except InvalidInput as exc:
            warning = t(lang, exc.key, **exc.params)
    text = render(
        lang, company_name=company.name, number=None, issue_date=None, customer=data["cname"],
        building=data["bname"], kind=data["kind"], currency=data["cur"],
        items=[(d, int(q), int(p)) for d, q, p in items], totals=totals, notes=data.get("notes"),
    )
    if warning:
        text += "\n\n⚠️ " + warning
    return views.invoice_draft(lang, text, bool(items), totals is not None)


def _check_amounts(svc: InvoiceService, data: dict, discount, tax) -> None:
    """Early feedback: the amounts must work with the items entered so far."""
    validate_field("discount", discount)
    validate_field("tax", tax)
    if data.get("items"):
        svc.draft_totals(data["items"], discount, tax)


def build_router() -> Router:
    router = Router()

    # ---------- lists and cards ----------

    @router.callback_query(F.data == "i:menu")
    async def menu(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        await cb.answer()
        await state.clear()
        await _show(cb, *views.invoices_home(lang, svc.default_currency()))

    @router.callback_query(F.data.startswith("i:list:"))
    async def invoice_list(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        parts = cb.data.split(":")
        if len(parts) != 4 or parts[2] not in ("all", "mine"):
            raise InvalidInput()
        if parts[2] == "all":
            svc.require_manager()  # customers only have "mine"
        elif svc.actor.role is not Role.CUSTOMER:
            raise InvalidInput()
        page = _int(cb.data, 3)
        rows, total = await svc.list_invoices(page)
        await cb.answer()
        await _show(cb, *views.invoices_list(lang, parts[2], rows, total, max(page, 0)))

    async def _card(svc: InvoiceService, lang: str, invoice_id: int):
        details = await svc.get_details(invoice_id)
        return views.invoice_card(lang, svc.text(lang, details), details.invoice, svc.actor)

    @router.callback_query(F.data.startswith("i:view:"))
    async def invoice_view(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _context(session, company, cb.from_user.id)
        view = await _card(svc, lang, _int(cb.data))
        await cb.answer()
        await _show(cb, *view)

    @router.callback_query(F.data.startswith("i:resend:"))
    async def resend(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        delivered = await svc.resend(_int(cb.data))
        await cb.answer(t(lang, "msg_invoice_sent" if delivered else "msg_invoice_unreachable"), show_alert=True)

    @router.callback_query(F.data.startswith("i:cx:"))
    async def cancel_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        details = await svc.get_details(_int(cb.data))
        await cb.answer()
        await _show(cb, *views.confirm_cancel_invoice(lang, details.invoice))

    @router.callback_query(F.data.startswith("i:cx_ok:"))
    async def cancel(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        invoice_id = _int(cb.data)
        await svc.cancel(invoice_id)
        view = await _card(svc, lang, invoice_id)
        await cb.answer(t(lang, "msg_invoice_cancelled"))
        await _show(cb, *view)

    # ---------- default currency ----------

    @router.callback_query(F.data == "i:dcs")
    async def default_currency_ask(cb: CallbackQuery, session: AsyncSession, company: Company):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        await cb.answer()
        await _show(cb, *views.currency_picker(lang, "i:dc", "pick_default_currency", "i:menu"))

    @router.callback_query(F.data.startswith("i:dc:"))
    async def default_currency_set(cb: CallbackQuery, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        currency = await svc.set_default_currency(cb.data.split(":")[2])
        await cb.answer(t(lang, "msg_default_currency_set"))
        await _show(cb, *views.invoices_home(lang, currency))

    # ---------- building a new invoice ----------

    @router.callback_query(F.data == "i:new")
    async def new_invoice(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _pick_customer(cb, state, session, company, 0)

    @router.callback_query(F.data.startswith("i:pc:"))
    async def customer_page(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _pick_customer(cb, state, session, company, _int(cb.data))

    async def _pick_customer(cb, state, session, company, page):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        actor = await AuthService(session, company, None).get_by_telegram_id(cb.from_user.id)
        customers, total = await DirectoryService(session, company, actor).list_users(Role.CUSTOMER, page)
        await cb.answer()
        await state.clear()
        await _show(cb, *views.invoice_customer_picker(lang, customers, total, max(page, 0)))

    @router.callback_query(F.data.startswith("i:pb:"))
    async def pick_building(cb: CallbackQuery, session: AsyncSession, company: Company):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        actor = await AuthService(session, company, None).get_by_telegram_id(cb.from_user.id)
        buildings = await DirectoryService(session, company, actor).list_buildings(_int(cb.data))
        await cb.answer()
        await _show(cb, *views.invoice_building_picker(lang, buildings))

    @router.callback_query(F.data.startswith("i:b:"))
    async def building_chosen(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        actor = await AuthService(session, company, None).get_by_telegram_id(cb.from_user.id)
        directory = DirectoryService(session, company, actor)
        building = await directory.get_building(_int(cb.data))
        customer = await directory.get_user(building.owner_id)
        await cb.answer()
        await state.clear()
        await state.update_data(
            cust=customer.id, cname=customer.name, bld=building.id, bname=building.name, items=[],
        )
        await _show(cb, *views.invoice_kind_picker(lang))

    @router.callback_query(F.data.startswith("i:k:"))
    async def kind_chosen(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        data = await state.get_data()
        if "cust" not in data:
            raise InvalidInput("err_draft_lost")
        kind = InvoiceKind(cb.data.split(":")[2]) if cb.data.split(":")[2] in {k.value for k in InvoiceKind} else None
        if kind is None:
            raise InvalidInput()
        await state.update_data(kind=kind.value)
        default = svc.default_currency()
        await cb.answer()
        if default is None:  # the first invoice: ask once for the company's default currency
            await _show(cb, *views.currency_picker(lang, "i:dcf", "pick_default_currency", "i:drop"))
            return
        await state.update_data(cur=default.value)
        await _show(cb, *_draft_view(svc, company, lang, await state.get_data()))

    @router.callback_query(F.data.startswith("i:dcf:"))
    async def first_currency(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        await _draft(state)
        currency = await svc.set_default_currency(cb.data.split(":")[2])
        await state.update_data(cur=currency.value)
        await cb.answer(t(lang, "msg_default_currency_set"))
        await _show(cb, *_draft_view(svc, company, lang, await state.get_data()))

    @router.callback_query(F.data == "i:cur")
    async def currency_ask(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        await _draft(state)
        await cb.answer()
        await _show(cb, *views.currency_picker(lang, "i:setcur", "pick_currency", "i:hub"))

    @router.callback_query(F.data.startswith("i:setcur:"))
    async def currency_set(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        await _draft(state)
        try:
            currency = Currency(cb.data.split(":")[2])
        except ValueError:
            raise InvalidInput() from None
        await state.update_data(cur=currency.value)
        await cb.answer()
        await _show(cb, *_draft_view(svc, company, lang, await state.get_data()))

    @router.callback_query(F.data == "i:hub")
    async def hub(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        data = await _draft(state)
        await cb.answer()
        await _show(cb, *_draft_view(svc, company, lang, data))

    @router.callback_query(F.data == "i:drop")
    async def drop(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        await cb.answer()
        await state.clear()
        await _show(cb, *views.invoices_home(lang, svc.default_currency()))

    # ---------- draft input steps ----------

    async def _ask(cb: CallbackQuery, state: FSMContext, session, company, step: State, prompt: str):
        _, lang = await _manager_context(session, company, cb.from_user.id)
        await _draft(state)
        await cb.answer()
        await state.set_state(step)
        await cb.message.answer(t(lang, prompt) + "\n" + t(lang, "cancel_hint"))

    @router.callback_query(F.data == "i:add")
    async def add_item(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _manager_context(session, company, cb.from_user.id)  # role first, then the draft
        data = await _draft(state)
        if len(data.get("items", [])) >= MAX_ITEMS:
            raise InvalidInput("err_invoice_items", max=MAX_ITEMS)
        await _ask(cb, state, session, company, Draft.desc, "prompt_item_desc")

    @router.callback_query(F.data == "i:disc")
    async def ask_discount(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _ask(cb, state, session, company, Draft.discount, "prompt_discount")

    @router.callback_query(F.data == "i:tax")
    async def ask_tax(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _ask(cb, state, session, company, Draft.tax, "prompt_tax")

    @router.callback_query(F.data == "i:notes")
    async def ask_notes(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        await _ask(cb, state, session, company, Draft.notes, "prompt_invoice_notes")

    @router.callback_query(F.data == "i:rm")
    async def remove_item(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        data = await _draft(state)
        await state.update_data(items=data.get("items", [])[:-1])
        await cb.answer()
        await _show(cb, *_draft_view(svc, company, lang, await state.get_data()))

    async def _step(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        actor = await AuthService(session, company, None).get_by_telegram_id(message.from_user.id)
        svc = InvoiceService(session, company, actor)
        svc.require_manager()
        return svc, (actor.language or company.default_language), await _draft(state)

    async def _back_to_draft(message: Message, state: FSMContext, svc, company, lang):
        await state.set_state(None)
        text, markup = _draft_view(svc, company, lang, await state.get_data())
        await message.answer(text, reply_markup=markup)

    _TEXT = F.text & ~F.text.startswith("/")

    @router.message(Draft.desc, _TEXT)
    async def got_desc(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        _, lang, _ = await _step(message, state, session, company)
        await state.update_data(pdesc=validate_field("item_desc", message.text))
        await state.set_state(Draft.qty)
        await message.answer(t(lang, "prompt_item_qty"))

    @router.message(Draft.qty, _TEXT)
    async def got_qty(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        _, lang, _ = await _step(message, state, session, company)
        await state.update_data(pqty=str(validate_field("item_qty", message.text)))
        await state.set_state(Draft.price)
        await message.answer(t(lang, "prompt_item_price"))

    @router.message(Draft.price, _TEXT)
    async def got_price(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, data = await _step(message, state, session, company)
        price = validate_field("item_price", message.text)
        items = [*data.get("items", []), [data["pdesc"], data["pqty"], str(price)]]
        if len(items) > MAX_ITEMS:
            raise InvalidInput("err_invoice_items", max=MAX_ITEMS)
        await state.update_data(items=items, pdesc=None, pqty=None)
        await _back_to_draft(message, state, svc, company, lang)

    @router.message(Draft.discount, _TEXT)
    async def got_discount(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, data = await _step(message, state, session, company)
        discount = None if message.text.strip() == "-" else message.text.strip()
        _check_amounts(svc, data, discount, data.get("tax"))
        await state.update_data(discount=discount)
        await _back_to_draft(message, state, svc, company, lang)

    @router.message(Draft.tax, _TEXT)
    async def got_tax(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, data = await _step(message, state, session, company)
        tax = None if message.text.strip() == "-" else message.text.strip()
        _check_amounts(svc, data, data.get("discount"), tax)
        await state.update_data(tax=tax)
        await _back_to_draft(message, state, svc, company, lang)

    @router.message(Draft.notes, _TEXT)
    async def got_notes(message: Message, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang, _ = await _step(message, state, session, company)
        await state.update_data(notes=validate_field("invoice_notes", message.text))
        await _back_to_draft(message, state, svc, company, lang)

    @router.message(Draft.desc, ~F.text)
    @router.message(Draft.qty, ~F.text)
    @router.message(Draft.price, ~F.text)
    @router.message(Draft.discount, ~F.text)
    @router.message(Draft.tax, ~F.text)
    @router.message(Draft.notes, ~F.text)
    async def not_text(message: Message, company: Company):
        await message.answer(t(company.default_language, "send_text"))

    # ---------- issue ----------

    @router.callback_query(F.data == "i:issue")
    async def issue(cb: CallbackQuery, state: FSMContext, session: AsyncSession, company: Company):
        svc, lang = await _manager_context(session, company, cb.from_user.id)
        data = await _draft(state)
        result = await svc.issue(
            data["cust"], data["bld"], data["kind"], data["cur"], [tuple(i) for i in data.get("items", [])],
            data.get("discount"), data.get("tax"), data.get("notes"),
        )
        await state.clear()
        view = await _card(svc, lang, result.invoice.id)
        await cb.answer(t(lang, "msg_invoice_issued" if result.queued else "msg_invoice_unreachable"),
                        show_alert=not result.queued)
        await _show(cb, *view)

    return router
