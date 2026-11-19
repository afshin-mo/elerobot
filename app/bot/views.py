"""Pure functions that build message text and keyboards (no I/O)."""
from datetime import datetime

from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Markup

from app.core.i18n import t
from app.core.timeutil import LOCAL_TZ
from app.models import (
    Building, Contract, ContractStatus, Currency, Elevator, Invoice, InvoiceKind, InvoiceStatus, Lead,
    LeadStatus, RepeatUnit, ReviewStatus, Role, ServiceVisit, TicketStatus, User, VisitStatus,
)
from app.services.contracts import ContractDetails
from app.services.directory import PAGE_SIZE
from app.services.invoice_format import money
from app.services.visits import VisitDetails
from app.services.tickets import TicketDetails, TicketRow

LRM = "\u200e"  # keeps phone numbers and dates readable inside right-to-left text
DASH = "—"


def _kb(rows: list[list[Btn]]) -> Markup:
    return Markup(inline_keyboard=rows)


def _status(lang: str, active: bool) -> str:
    return t(lang, "status_active" if active else "status_inactive")


def _toggle_button(lang: str, prefix: str, entity_id: int, active: bool) -> Btn:
    key, target = ("btn_deactivate", 0) if active else ("btn_activate", 1)
    return Btn(text=t(lang, key), callback_data=f"{prefix}:act:{entity_id}:{target}")


def _edit_button(lang: str, prefix: str, entity_id: int, field: str, short: str) -> Btn:
    return Btn(text=t(lang, f"btn_edit_{short}"), callback_data=f"{prefix}:edit:{entity_id}:{field}")


_TITLES = {Role.CUSTOMER: "title_customers", Role.TECHNICIAN: "title_technicians", Role.MANAGER: "title_managers"}
_ADD_BUTTONS = {Role.CUSTOMER: "btn_add_customer", Role.TECHNICIAN: "btn_add_technician", Role.MANAGER: "btn_add_manager"}


def main_menu(user: User) -> tuple[str, Markup]:
    """Text and inline menu shown after sign-in, per role."""
    lang = user.language
    text = t(lang, "main_menu", role=t(lang, f"role_{user.role.value}"))
    if user.role is Role.MANAGER:
        rows = [
            [Btn(text=t(lang, "btn_tickets"), callback_data="t:groups")],
            [Btn(text=t(lang, "btn_customers"), callback_data="u:list:customer:0")],
            [Btn(text=t(lang, "btn_technicians"), callback_data="u:list:technician:0")],
            [Btn(text=t(lang, "btn_contracts"), callback_data="c:menu")],
            [Btn(text=t(lang, "btn_visits"), callback_data="v:menu")],
            [Btn(text=t(lang, "btn_invoices"), callback_data="i:menu")],
            [Btn(text=t(lang, "btn_leads"), callback_data="l:list:0")],
        ]
        if user.is_admin:  # only the admin manages managers
            rows.append([Btn(text=t(lang, "btn_managers"), callback_data="u:list:manager:0")])
    elif user.role is Role.CUSTOMER:
        rows = [
            [Btn(text=t(lang, "btn_report_breakdown"), callback_data="t:new")],
            [Btn(text=t(lang, "btn_my_tickets"), callback_data="t:list:mine:0")],
            [Btn(text=t(lang, "btn_my_contracts"), callback_data="c:list:mine:0")],
            [Btn(text=t(lang, "btn_my_visits"), callback_data="v:list:mine:0")],
            [Btn(text=t(lang, "btn_my_invoices"), callback_data="i:list:mine:0")],
        ]
    else:
        rows = [[Btn(text=t(lang, "btn_my_tasks"), callback_data="t:list:tasks:0")]]
    return text, _kb(rows)


def users_list(lang: str, role: Role, users: list[User], total: int, page: int):
    text = f"{t(lang, _TITLES[role])} ({total})"
    if not users:
        text += "\n\n" + t(lang, "empty_list")
    rows = [
        [Btn(text=u.name + ("" if u.is_active else " ⛔"), callback_data=f"u:view:{u.id}")]
        for u in users
    ]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"u:list:{role.value}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"u:list:{role.value}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(text=t(lang, _ADD_BUTTONS[role]), callback_data=f"u:add:{role.value}")])
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu")])
    return text, _kb(rows)


def user_card(lang: str, user: User, buildings: list[Building], viewer_id: int):
    linked = t(lang, "telegram_linked" if user.telegram_id else "telegram_not_linked")
    lines = [
        user.name + (" 👑" if user.is_admin else ""),
        f"{t(lang, 'label_phone')}: {LRM}{user.phone}{LRM}",
        f"{t(lang, 'label_telegram')}: {linked}",
        f"{t(lang, 'label_status')}: {_status(lang, user.is_active)}",
    ]
    rows: list[list[Btn]] = []
    if user.role is Role.CUSTOMER:
        lines += ["", f"{t(lang, 'label_buildings')}:" + ("" if buildings else " " + t(lang, "no_buildings"))]
        rows += [
            [Btn(text=b.name + ("" if b.is_active else " ⛔"), callback_data=f"b:view:{b.id}")]
            for b in buildings
        ]
        if user.is_active:
            rows.append([Btn(text=t(lang, "btn_add_building"), callback_data=f"b:add:{user.id}")])
            rows.append([Btn(text=t(lang, "btn_report_for_customer"), callback_data=f"t:new_for:{user.id}")])
    rows.append([
        _edit_button(lang, "u", user.id, "user_name", "name"),
        _edit_button(lang, "u", user.id, "phone", "phone"),
    ])
    if user.id != viewer_id:  # nobody deactivates or unlinks their own account
        rows.append([_toggle_button(lang, "u", user.id, user.is_active)])
    if user.telegram_id and user.id != viewer_id:
        rows.append([Btn(text=t(lang, "btn_unlink_telegram"), callback_data=f"u:unlink:{user.id}")])
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=f"u:list:{user.role.value}:0")])
    return "\n".join(lines), _kb(rows)


def confirm_unlink(lang: str, user: User):
    rows = [[
        Btn(text=t(lang, "btn_yes"), callback_data=f"u:unlink_ok:{user.id}"),
        Btn(text=t(lang, "btn_no"), callback_data=f"u:view:{user.id}"),
    ]]
    return t(lang, "confirm_unlink", name=user.name), _kb(rows)


def building_card(lang: str, building: Building, owner: User, elevators: list[Elevator]):
    lines = [
        building.name,
        f"{t(lang, 'label_owner')}: {owner.name}",
        f"{t(lang, 'label_address')}: {building.address}",
        f"{t(lang, 'label_notes')}: {building.notes or DASH}",
        f"{t(lang, 'label_status')}: {_status(lang, building.is_active)}",
        "",
        f"{t(lang, 'label_elevators')}:" + ("" if elevators else " " + t(lang, "no_elevators")),
    ]
    rows = [
        [Btn(text=e.name + ("" if e.is_active else " ⛔"), callback_data=f"e:view:{e.id}")]
        for e in elevators
    ]
    if building.is_active:
        rows.append([Btn(text=t(lang, "btn_add_elevator"), callback_data=f"e:add:{building.id}")])
    rows.append([Btn(text=t(lang, "btn_contracts_building"), callback_data=f"c:bl:{building.id}")])
    rows.append([Btn(text=t(lang, "btn_new_invoice"), callback_data=f"i:b:{building.id}")])
    rows.append([
        _edit_button(lang, "b", building.id, "building_name", "name"),
        _edit_button(lang, "b", building.id, "address", "address"),
        _edit_button(lang, "b", building.id, "notes", "notes"),
    ])
    rows.append([_toggle_button(lang, "b", building.id, building.is_active)])
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=f"u:view:{building.owner_id}")])
    return "\n".join(lines), _kb(rows)


def elevator_card(lang: str, elevator: Elevator, building: Building):
    install = f"{LRM}{elevator.install_date.isoformat()}{LRM}" if elevator.install_date else DASH
    lines = [
        elevator.name,
        f"{t(lang, 'label_building')}: {building.name}",
        f"{t(lang, 'label_brand')}: {elevator.brand or DASH}",
        f"{t(lang, 'label_install_date')}: {install}",
        f"{t(lang, 'label_notes')}: {elevator.notes or DASH}",
        f"{t(lang, 'label_status')}: {_status(lang, elevator.is_active)}",
    ]
    rows = [
        [
            _edit_button(lang, "e", elevator.id, "elevator_name", "name"),
            _edit_button(lang, "e", elevator.id, "brand", "brand"),
        ],
        [
            _edit_button(lang, "e", elevator.id, "install_date", "install_date"),
            _edit_button(lang, "e", elevator.id, "notes", "notes"),
        ],
        [Btn(text=t(lang, "btn_visits_elevator"), callback_data=f"v:el:{elevator.id}")],
        [_toggle_button(lang, "e", elevator.id, elevator.is_active)],
        [Btn(text=t(lang, "btn_back"), callback_data=f"b:view:{elevator.building_id}")],
    ]
    return "\n".join(lines), _kb(rows)


# ---- tickets ----

_STATUS_GROUP = {
    TicketStatus.NEW: "new", TicketStatus.ASSIGNED: "active", TicketStatus.IN_PROGRESS: "active",
    TicketStatus.REPORTED: "review", TicketStatus.CLOSED: "done", TicketStatus.CANCELLED: "done",
}


def _when(value: datetime | None) -> str:
    return DASH if value is None else LRM + value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") + LRM


def _ts(lang: str, status: TicketStatus) -> str:
    return t(lang, f"ts_{status.value}")


def tickets_home(lang: str, counts: dict[str, int]):
    rows = [
        [Btn(text=f"{t(lang, 'grp_' + group)} ({counts[group]})", callback_data=f"t:list:{group}:0")]
        for group in ("new", "active", "review", "done")
    ]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu")])
    return t(lang, "btn_tickets"), _kb(rows)


def ticket_list(lang: str, group: str, rows: list[TicketRow], total: int, page: int):
    title = {"mine": "title_my_tickets", "tasks": "title_my_tasks"}.get(group, f"grp_{group}")
    text = f"{t(lang, title)} ({total})"
    if not rows:
        text += "\n\n" + t(lang, "no_tickets")
    kb = [
        [Btn(
            text=f"#{r.ticket.id} · {r.building.name} / {r.elevator.name} · {_ts(lang, r.ticket.status)}",
            callback_data=f"t:view:{r.ticket.id}",
        )]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"t:list:{group}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"t:list:{group}:{page + 1}"))
    if nav:
        kb.append(nav)
    back = "t:groups" if group in ("new", "active", "review", "done") else "m:menu"
    kb.append([Btn(text=t(lang, "btn_back"), callback_data=back)])
    return text, _kb(kb)


def ticket_card(lang: str, d: TicketDetails, viewer: User):
    ticket, staff = d.ticket, viewer.role is not Role.CUSTOMER
    lines = [f"🎫 #{ticket.id} — {_ts(lang, ticket.status)}"]
    if staff:
        lines.append(f"{t(lang, 'label_owner')}: {d.customer.name} {LRM}{d.customer.phone}{LRM}")
        lines.append(f"{t(lang, 'label_building')}: {d.building.name} — {d.building.address}")
    else:
        lines.append(f"{t(lang, 'label_building')}: {d.building.name}")
    lines += [
        f"{t(lang, 'label_elevator')}: {d.elevator.name}",
        f"{t(lang, 'label_description')}: {ticket.description}",
    ]
    if viewer.role is Role.MANAGER:
        lines.append(f"{t(lang, 'label_technician')}: {d.technician.name if d.technician else DASH}")
    lines.append(f"{t(lang, 'label_created')}: {_when(ticket.created_at)}")
    if d.photos:
        lines.append(f"{t(lang, 'label_photos')}: {len(d.photos)}")
    if d.feedback is not None:
        stars = "⭐" * d.feedback.rating
        line = f"{t(lang, 'label_feedback')}: {stars} ({d.feedback.rating}/5)"
        lines += ["", line + (f"\n{d.feedback.comment}" if d.feedback.comment else "")]
    report = d.latest_report
    if report is not None:
        lines += ["", f"{t(lang, 'label_report')}: {report.text}",
                  f"{t(lang, 'label_review')}: {t(lang, 'rs_' + report.review_status.value)}"]
        if report.review_note:
            lines.append(f"{t(lang, 'label_manager_note')}: {report.review_note}")

    rows: list[list[Btn]] = []
    tid = ticket.id
    if viewer.role is Role.MANAGER:
        if ticket.status is TicketStatus.NEW:
            rows.append([Btn(text=t(lang, "btn_assign"), callback_data=f"t:assign:{tid}")])
        elif ticket.status in (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS):
            rows.append([Btn(text=t(lang, "btn_change_technician"), callback_data=f"t:assign:{tid}")])
        elif ticket.status is TicketStatus.REPORTED and report is not None:
            first = ("btn_approve", "t:approve") if report.review_status is ReviewStatus.PENDING \
                else ("btn_close_ticket", "t:close")
            rows.append([
                Btn(text=t(lang, first[0]), callback_data=f"{first[1]}:{tid}"),
                Btn(text=t(lang, "btn_reject"), callback_data=f"t:reject:{tid}"),
            ])
        if ticket.status in (TicketStatus.NEW, TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS,
                             TicketStatus.REPORTED):
            rows.append([Btn(text=t(lang, "btn_cancel_ticket"), callback_data=f"t:cancel:{tid}")])
        back = f"t:list:{_STATUS_GROUP[ticket.status]}:0"
    elif viewer.role is Role.TECHNICIAN:
        if ticket.status is TicketStatus.ASSIGNED:
            rows.append([Btn(text=t(lang, "btn_start_work"), callback_data=f"t:start:{tid}")])
        elif ticket.status is TicketStatus.IN_PROGRESS:
            rows.append([Btn(text=t(lang, "btn_submit_report"), callback_data=f"t:report:{tid}")])
        back = "t:list:tasks:0"
    else:
        back = "t:list:mine:0"
    if d.feedback is None and ticket.status in (TicketStatus.REPORTED, TicketStatus.CLOSED):
        if viewer.role is Role.MANAGER:
            rows.append([Btn(text=t(lang, "btn_request_feedback"), callback_data=f"t:askfb:{tid}")])
        elif viewer.role is Role.CUSTOMER:
            rows.append([Btn(text=t(lang, "btn_send_feedback"), callback_data=f"t:feedback:{tid}")])
    if d.photos:
        rows.append([Btn(text=t(lang, "btn_view_photos"), callback_data=f"t:photos:{tid}")])
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=back)])
    return "\n".join(lines), _kb(rows)


def elevator_picker(lang: str, pairs, back: str):
    rows = [
        [Btn(text=f"{b.name} / {e.name}", callback_data=f"t:pick:{e.id}")] for e, b in pairs
    ]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=back)])
    return t(lang, "prompt_pick_elevator"), _kb(rows)


def technician_picker(lang: str, ticket_id: int, technicians: list[User]):
    rows = [[Btn(text=u.name, callback_data=f"t:set:{ticket_id}:{u.id}")] for u in technicians]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=f"t:view:{ticket_id}")])
    return t(lang, "prompt_pick_technician"), _kb(rows)


def ticket_summary(lang: str, elevator: Elevator, building: Building, description: str, has_photo: bool):
    lines = [
        t(lang, "summary_title"),
        f"{t(lang, 'label_building')}: {building.name}",
        f"{t(lang, 'label_elevator')}: {elevator.name}",
        f"{t(lang, 'label_description')}: {description}",
    ]
    if has_photo:
        lines.append(f"{t(lang, 'label_photos')}: 1")
    rows = [[
        Btn(text=t(lang, "btn_confirm"), callback_data="t:confirm"),
        Btn(text=t(lang, "btn_cancel"), callback_data="m:menu"),
    ]]
    return "\n".join(lines), _kb(rows)


def confirm_cancel_ticket(lang: str, ticket_id: int):
    rows = [[
        Btn(text=t(lang, "btn_yes"), callback_data=f"t:cancel_ok:{ticket_id}"),
        Btn(text=t(lang, "btn_no"), callback_data=f"t:view:{ticket_id}"),
    ]]
    return t(lang, "confirm_cancel_ticket", id=ticket_id), _kb(rows)


def rating_picker(lang: str, ticket_id: int):
    stars = [Btn(text="⭐" * n, callback_data=f"t:rate:{ticket_id}:{n}") for n in range(1, 6)]
    rows = [stars[:3], stars[3:], [Btn(text=t(lang, "btn_back"), callback_data=f"t:view:{ticket_id}")]]
    return t(lang, "prompt_rating"), _kb(rows)


# ---- guest mode (not signed in) ----

GUEST_INFO = ("about", "services", "prices", "contact")


def guest_menu(lang: str, company_name: str):
    """Public menu for people who are not signed in. Callback data carries the language,
    so the screens keep working even after a bot restart."""
    rows = [
        [Btn(text=t(lang, f"btn_guest_{name}"), callback_data=f"g:{name}:{lang}")]
        for name in (*GUEST_INFO, "consult")
    ]
    rows.append([Btn(text=t(lang, "btn_guest_login"), callback_data=f"g:login:{lang}")])
    return t(lang, "guest_menu", company=company_name), _kb(rows)


def guest_info(lang: str, name: str, company_name: str):
    kb = [[Btn(text=t(lang, "btn_back"), callback_data=f"g:menu:{lang}")]]
    if name != "contact":
        kb.insert(0, [Btn(text=t(lang, "btn_guest_consult"), callback_data=f"g:consult:{lang}")])
    return t(lang, f"guest_{name}", company=company_name), _kb(kb)


# ---- consultation requests (managers) ----


def _lead_status(lang: str, status: LeadStatus) -> str:
    return t(lang, f"ls_{status.value}")


def leads_list(lang: str, leads: list[Lead], total: int, new: int, page: int):
    text = t(lang, "title_leads", total=total, new=new)
    if not leads:
        text += "\n\n" + t(lang, "empty_list")
    rows = [
        [Btn(text=f"#{x.id} · {x.name} · {_lead_status(lang, x.status)}", callback_data=f"l:view:{x.id}")]
        for x in leads
    ]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"l:list:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"l:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu")])
    return text, _kb(rows)


def lead_card(lang: str, lead: Lead, handler: User | None):
    handled = ""
    if handler is not None:
        handled = "\n" + t(lang, "lead_handled_by", name=handler.name, when=_when(lead.handled_at))
    text = t(
        lang, "lead_card", id=lead.id, status=_lead_status(lang, lead.status), name=lead.name,
        phone=LRM + lead.phone + LRM,
        username=f"@{lead.telegram_username}" if lead.telegram_username else DASH,
        when=_when(lead.created_at), message=lead.message, handled=handled,
    )
    rows = []
    if lead.status is LeadStatus.NEW:
        rows.append([Btn(text=t(lang, "btn_lead_contacted"), callback_data=f"l:set:{lead.id}:contacted")])
    if lead.status is not LeadStatus.CLOSED:
        rows.append([Btn(text=t(lang, "btn_lead_closed"), callback_data=f"l:set:{lead.id}:closed")])
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="l:list:0")])
    return text, _kb(rows)


# ---- contracts ----


def _day(value) -> str:
    return LRM + value.isoformat() + LRM


def contract_state(lang: str, contract: Contract, today) -> str:
    if contract.status is ContractStatus.CANCELLED:
        return t(lang, "cs_cancelled")
    days = (contract.end_date - today).days
    if days < 0:
        return t(lang, "cs_expired")
    return t(lang, "cs_active") + " · " + t(lang, "contract_days_left", days=days)


def contracts_home(lang: str, counts: dict[str, int]):
    rows = [
        [Btn(text=f"{t(lang, 'cg_' + group)} ({counts[group]})", callback_data=f"c:list:{group}:0")]
        for group in ("expiring", "active", "ended")
    ]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu")])
    return t(lang, "btn_contracts"), _kb(rows)


def contracts_list(lang: str, group: str, rows: list[tuple[Contract, Building]], total: int, page: int):
    title = "title_my_contracts" if group == "mine" else f"cg_{group}"
    text = f"{t(lang, title)} ({total})"
    if not rows:
        text += "\n\n" + t(lang, "no_contracts")
    kb = [
        [Btn(text=f"#{c.id} · {b.name} · {_day(c.end_date)}", callback_data=f"c:view:{c.id}")]
        for c, b in rows
    ]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"c:list:{group}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"c:list:{group}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu" if group == "mine" else "c:menu")])
    return text, _kb(kb)


def building_contracts(lang: str, building: Building, contracts: list[Contract], today):
    text = f"{t(lang, 'btn_contracts_building')} — {building.name}"
    if not contracts:
        text += "\n\n" + t(lang, "no_contracts")
    kb = [
        [Btn(
            text=f"#{c.id} · {_day(c.start_date)} → {_day(c.end_date)} · {t(lang, 'cs_' + _state_key(c, today))}",
            callback_data=f"c:view:{c.id}",
        )]
        for c in contracts
    ]
    if building.is_active:
        kb.append([Btn(text=t(lang, "btn_add_contract"), callback_data=f"c:add:{building.id}")])
    kb.append([Btn(text=t(lang, "btn_back"), callback_data=f"b:view:{building.id}")])
    return text, _kb(kb)


def _state_key(contract: Contract, today) -> str:
    if contract.status is ContractStatus.CANCELLED:
        return "cancelled"
    return "expired" if contract.end_date < today else "active"


def contract_card(lang: str, d: ContractDetails, viewer: User, today):
    c = d.contract
    lines = [
        t(lang, "label_contract", id=c.id),
        f"{t(lang, 'label_building')}: {d.building.name}",
        f"{t(lang, 'label_owner')}: {d.owner.name}",
        f"{t(lang, 'label_period')}: {_day(c.start_date)} → {_day(c.end_date)}",
        f"{t(lang, 'label_status')}: {contract_state(lang, c, today)}",
        f"{t(lang, 'label_notes')}: {c.notes or DASH}",
    ]
    rows = []
    active = c.status is ContractStatus.ACTIVE
    if viewer.role is Role.MANAGER:
        if active:
            rows.append([
                _edit_button(lang, "c", c.id, "end_date", "end_date"),
                _edit_button(lang, "c", c.id, "contract_notes", "notes"),
            ])
            rows.append([Btn(text=t(lang, "btn_cancel_contract"), callback_data=f"c:cancel:{c.id}")])
        rows.append([Btn(text=t(lang, "btn_back"), callback_data=f"c:bl:{d.building.id}")])
    else:
        if active and c.end_date >= today:
            rows.append([Btn(text=t(lang, "btn_request_renewal"), callback_data=f"c:renew:{c.id}")])
        rows.append([Btn(text=t(lang, "btn_back"), callback_data="c:list:mine:0")])
    return "\n".join(lines), _kb(rows)


def confirm_cancel_contract(lang: str, contract_id: int):
    rows = [[
        Btn(text=t(lang, "btn_yes"), callback_data=f"c:cancel_ok:{contract_id}"),
        Btn(text=t(lang, "btn_no"), callback_data=f"c:view:{contract_id}"),
    ]]
    return t(lang, "confirm_cancel_contract", id=contract_id), _kb(rows)


# ---- service visits ----

_OPEN_STATES = (VisitStatus.SCHEDULED, VisitStatus.CONFIRMED, VisitStatus.CHANGE_REQUESTED)


def visit_state(lang: str, visit: ServiceVisit, now: datetime) -> str:
    if visit.status in (VisitStatus.SCHEDULED, VisitStatus.CONFIRMED) and visit.scheduled_at < now:
        return t(lang, "vs_overdue")
    return t(lang, f"vs_{visit.status.value}")


def repeat_text(lang: str, visit: ServiceVisit) -> str:
    if visit.repeat_every is None or visit.repeat_unit is None:
        return t(lang, "repeat_none")
    key = "repeat_days" if visit.repeat_unit is RepeatUnit.DAYS else "repeat_months"
    return t(lang, key, n=visit.repeat_every)


def visits_home(lang: str, counts: dict[str, int]):
    rows = [
        [Btn(text=f"{t(lang, 'vg_' + group)} ({counts[group]})", callback_data=f"v:list:{group}:0")]
        for group in ("attention", "upcoming", "history")
    ]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu")])
    return t(lang, "btn_visits"), _kb(rows)


def visits_list(lang: str, group: str, rows, total: int, page: int, now: datetime):
    title = "title_my_visits" if group == "mine" else f"vg_{group}"
    text = f"{t(lang, title)} ({total})"
    if not rows:
        text += "\n\n" + t(lang, "no_visits")
    kb = [
        [Btn(text=f"#{v.id} · {e.name} · {b.name} · {_when(v.scheduled_at)}", callback_data=f"v:view:{v.id}")]
        for v, e, b in rows
    ]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"v:list:{group}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"v:list:{group}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu" if group == "mine" else "v:menu")])
    return text, _kb(kb)


def elevator_visits(lang: str, elevator: Elevator, building: Building, visits: list[ServiceVisit], now: datetime):
    text = f"{t(lang, 'btn_visits_elevator')} — {elevator.name} ({building.name})"
    if not visits:
        text += "\n\n" + t(lang, "no_visits")
    kb = [
        [Btn(text=f"#{v.id} · {_when(v.scheduled_at)} · {visit_state(lang, v, now)}", callback_data=f"v:view:{v.id}")]
        for v in visits
    ]
    if elevator.is_active and building.is_active:
        kb.append([Btn(text=t(lang, "btn_add_visit"), callback_data=f"v:add:{elevator.id}")])
    kb.append([Btn(text=t(lang, "btn_back"), callback_data=f"e:view:{elevator.id}")])
    return text, _kb(kb)


def visit_card(lang: str, d: VisitDetails, viewer: User, now: datetime):
    v = d.visit
    lines = [
        t(lang, "label_visit", id=v.id),
        f"{t(lang, 'label_elevator')}: {d.elevator.name} ({d.building.name})",
        f"{t(lang, 'label_owner')}: {d.owner.name}",
        f"{t(lang, 'label_time')}: {_when(v.scheduled_at)}",
        f"{t(lang, 'label_status')}: {visit_state(lang, v, now)}",
        f"{t(lang, 'label_repeat')}: {repeat_text(lang, v)}",
    ]
    if v.customer_note:
        lines.append(f"{t(lang, 'label_customer_note')}: {v.customer_note}")
    is_open = v.status in _OPEN_STATES
    rows = []
    if viewer.role is Role.MANAGER:
        if is_open:
            rows.append([
                _edit_button(lang, "v", v.id, "visit_time", "visit_time"),
                _edit_button(lang, "v", v.id, "visit_repeat", "visit_repeat"),
            ])
            rows.append([
                Btn(text=t(lang, "btn_visit_done"), callback_data=f"v:done:{v.id}"),
                Btn(text=t(lang, "btn_visit_cancel"), callback_data=f"v:cx:{v.id}"),
            ])
        rows.append([Btn(text=t(lang, "btn_back"), callback_data=f"v:el:{d.elevator.id}")])
    else:
        future = v.scheduled_at > now
        if is_open and future:
            first = []
            if v.status is VisitStatus.SCHEDULED:
                first.append(Btn(text=t(lang, "btn_visit_confirm"), callback_data=f"v:ok:{v.id}"))
            if v.status is not VisitStatus.CHANGE_REQUESTED:
                first.append(Btn(text=t(lang, "btn_visit_change"), callback_data=f"v:chg:{v.id}"))
            if first:
                rows.append(first)
            rows.append([Btn(text=t(lang, "btn_visit_cancel"), callback_data=f"v:cx:{v.id}")])
        rows.append([Btn(text=t(lang, "btn_back"), callback_data="v:list:mine:0")])
    return "\n".join(lines), _kb(rows)


def confirm_visit_action(lang: str, kind: str, visit_id: int):
    """kind: "done" or "cx" (cancel)"""
    text = t(lang, "confirm_visit_done" if kind == "done" else "confirm_cancel_visit", id=visit_id)
    rows = [[
        Btn(text=t(lang, "btn_yes"), callback_data=f"v:{kind}_ok:{visit_id}"),
        Btn(text=t(lang, "btn_no"), callback_data=f"v:view:{visit_id}"),
    ]]
    return text, _kb(rows)


# ---- invoices ----


def _cur(lang: str, currency) -> str:
    return t(lang, f"cur_{getattr(currency, 'value', currency)}")


def invoices_home(lang: str, default_currency: Currency | None):
    current = _cur(lang, default_currency) if default_currency else DASH
    rows = [
        [Btn(text=t(lang, "btn_new_invoice"), callback_data="i:new")],
        [Btn(text=t(lang, "btn_all_invoices"), callback_data="i:list:all:0")],
        [Btn(text=t(lang, "btn_default_currency", cur=current), callback_data="i:dcs")],
        [Btn(text=t(lang, "btn_back"), callback_data="m:menu")],
    ]
    return t(lang, "btn_invoices"), _kb(rows)


def invoices_list(lang: str, group: str, rows: list[tuple[Invoice, User]], total: int, page: int):
    text = f"{t(lang, 'title_my_invoices' if group == 'mine' else 'title_invoices')} ({total})"
    if not rows:
        text += "\n\n" + t(lang, "no_invoices")
    kb = []
    for inv, customer in rows:
        mark = " ⛔" if inv.status is InvoiceStatus.CANCELLED else ""
        who = "" if group == "mine" else f" · {customer.name}"
        label = f"{inv.number}{who} · {money(inv.total)} {inv.currency.value}{mark}"
        kb.append([Btn(text=label, callback_data=f"i:view:{inv.id}")])
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"i:list:{group}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"i:list:{group}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([Btn(text=t(lang, "btn_back"), callback_data="m:menu" if group == "mine" else "i:menu")])
    return text, _kb(kb)


def invoice_card(lang: str, text: str, invoice: Invoice, viewer: User):
    rows = []
    if viewer.role is Role.MANAGER:
        if invoice.status is InvoiceStatus.ISSUED:
            rows.append([
                Btn(text=t(lang, "btn_invoice_resend"), callback_data=f"i:resend:{invoice.id}"),
                Btn(text=t(lang, "btn_invoice_cancel"), callback_data=f"i:cx:{invoice.id}"),
            ])
        rows.append([Btn(text=t(lang, "btn_back"), callback_data="i:list:all:0")])
    else:
        rows.append([Btn(text=t(lang, "btn_back"), callback_data="i:list:mine:0")])
    return text, _kb(rows)


def confirm_cancel_invoice(lang: str, invoice: Invoice):
    rows = [[
        Btn(text=t(lang, "btn_yes"), callback_data=f"i:cx_ok:{invoice.id}"),
        Btn(text=t(lang, "btn_no"), callback_data=f"i:view:{invoice.id}"),
    ]]
    return t(lang, "confirm_cancel_invoice", number=invoice.number), _kb(rows)


def invoice_customer_picker(lang: str, customers: list[User], total: int, page: int):
    rows = [[Btn(text=c.name, callback_data=f"i:pb:{c.id}")] for c in customers]
    nav = []
    if page > 0:
        nav.append(Btn(text=t(lang, "btn_prev"), callback_data=f"i:pc:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Btn(text=t(lang, "btn_next"), callback_data=f"i:pc:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="i:menu")])
    text = t(lang, "pick_customer") + ("" if customers else "\n\n" + t(lang, "empty_list"))
    return text, _kb(rows)


def invoice_building_picker(lang: str, buildings: list[Building]):
    rows = [[Btn(text=b.name, callback_data=f"i:b:{b.id}")] for b in buildings]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data="i:pc:0")])
    text = t(lang, "pick_building") + ("" if buildings else "\n\n" + t(lang, "no_buildings"))
    return text, _kb(rows)


def invoice_kind_picker(lang: str):
    rows = [[Btn(text=t(lang, f"kind_{k.value}"), callback_data=f"i:k:{k.value}")] for k in InvoiceKind]
    rows.append([Btn(text=t(lang, "btn_discard"), callback_data="i:drop")])
    return t(lang, "pick_kind"), _kb(rows)


def currency_picker(lang: str, action: str, prompt_key: str, back: str):
    """action: callback prefix, e.g. "i:setcur" -> i:setcur:IQD"""
    rows = [[Btn(text=_cur(lang, c), callback_data=f"{action}:{c.value}")] for c in Currency]
    rows.append([Btn(text=t(lang, "btn_back"), callback_data=back)])
    return t(lang, prompt_key), _kb(rows)


def invoice_draft(lang: str, text: str, has_items: bool, can_issue: bool):
    rows = [[Btn(text=t(lang, "btn_add_item"), callback_data="i:add")]]
    if has_items:
        rows.append([Btn(text=t(lang, "btn_remove_item"), callback_data="i:rm")])
    rows.append([
        Btn(text=t(lang, "btn_set_currency"), callback_data="i:cur"),
        Btn(text=t(lang, "btn_set_notes"), callback_data="i:notes"),
    ])
    rows.append([
        Btn(text=t(lang, "btn_set_discount"), callback_data="i:disc"),
        Btn(text=t(lang, "btn_set_tax"), callback_data="i:tax"),
    ])
    if can_issue:
        rows.append([Btn(text=t(lang, "btn_issue_invoice"), callback_data="i:issue")])
    rows.append([Btn(text=t(lang, "btn_discard"), callback_data="i:drop")])
    return text, _kb(rows)
