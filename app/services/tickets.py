"""Breakdown ticket lifecycle (FR-2 to FR-7, FR-9).

new -> assigned -> in_progress -> reported -> closed, and cancelled from any open status.
Every status change is written to ticket_status_history. Only managers assign, review,
close and cancel; technicians start work and report; customers report and see their own.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.models import (
    Building, Company, Elevator, ReviewStatus, Role, Ticket, TicketFeedback, TicketReport,
    TicketStatus, User,
)
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.elevators import ElevatorRepository
from app.repositories.tickets import TicketRepository
from app.repositories.users import UserRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.permissions import require_role
from app.services.validation import validate_field, validate_rating

log = logging.getLogger(__name__)

S = TicketStatus
OPEN_STATUSES = (S.NEW, S.ASSIGNED, S.IN_PROGRESS, S.REPORTED)
MANAGER_GROUPS = {
    "new": (S.NEW,),
    "active": (S.ASSIGNED, S.IN_PROGRESS),
    "review": (S.REPORTED,),
    "done": (S.CLOSED, S.CANCELLED),
}
TECHNICIAN_TASK_STATUSES = (S.ASSIGNED, S.IN_PROGRESS)
FEEDBACK_STATUSES = (S.REPORTED, S.CLOSED)  # feedback makes sense once the work is reported


@dataclass(frozen=True)
class TicketRow:
    ticket: Ticket
    elevator: Elevator
    building: Building


@dataclass(frozen=True)
class TicketDetails:
    ticket: Ticket
    elevator: Elevator
    building: Building
    customer: User
    technician: User | None
    photos: list[str] = field(default_factory=list)
    reports: list[TicketReport] = field(default_factory=list)  # empty for customers
    feedback: TicketFeedback | None = None  # managers and the ticket's customer only

    @property
    def latest_report(self) -> TicketReport | None:
        return self.reports[-1] if self.reports else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TicketService:
    def __init__(
        self, session: AsyncSession, company: Company, actor: User | None,
        outbox: Outbox | None = None,
    ):
        self.actor = require_role(actor, Role.MANAGER, Role.TECHNICIAN, Role.CUSTOMER)
        self.company = company
        self.outbox = outbox  # tests pass one; the bot uses the per-update outbox
        self.tickets = TicketRepository(session)
        self.users = UserRepository(session)
        self.buildings = BuildingRepository(session)
        self.elevators = ElevatorRepository(session)
        self.audit = AuditRepository(session)

    # ---- creating ----

    async def reportable_elevators(self, customer_id: int | None = None):
        """(elevator, building) pairs the customer can report. Managers pass customer_id."""
        owner_id = await self._reporting_customer_id(customer_id)
        return await self.elevators.list_active_for_owner(self.company.id, owner_id)

    async def create_ticket(
        self, elevator_id: int, description: str, photo_file_id: str | None = None,
        customer_id: int | None = None,
    ) -> Ticket:
        owner_id = await self._reporting_customer_id(customer_id)
        elevator = await self.elevators.get(self.company.id, elevator_id)
        building = await self.buildings.get(self.company.id, elevator.building_id) if elevator else None
        if elevator is None or building is None or building.owner_id != owner_id:
            raise NotFound()
        if not elevator.is_active or not building.is_active:
            raise InvalidInput("err_inactive_parent")
        text = validate_field("description", description)
        if photo_file_id is not None and not 0 < len(photo_file_id) <= 255:
            raise InvalidInput()

        ticket = await self.tickets.add(Ticket(
            company_id=self.company.id, elevator_id=elevator.id, customer_id=owner_id,
            description=text, status=S.NEW,
        ))
        await self.tickets.add_history(self.company.id, ticket.id, None, S.NEW, self.actor.id)
        if photo_file_id:
            await self.tickets.add_attachment(self.company.id, ticket.id, photo_file_id)
        await self._log("ticket_created", ticket, on_behalf=self.actor.role is Role.MANAGER)
        customer = await self.users.get_by_id(self.company.id, owner_id)
        await self._notify_managers(
            "notif_new_ticket", ticket, customer=customer.name, building=building.name,
            elevator=elevator.name, description=text[:300],
        )
        if self.actor.role is Role.MANAGER:  # registered by phone on the customer's behalf
            self._emit(customer, "notif_status_new", ticket.id)
        return ticket

    # ---- reading ----

    async def counts(self) -> dict[str, int]:
        """Ticket counts per manager group."""
        self._manager_only()
        return {
            group: await self.tickets.count(self.company.id, statuses)
            for group, statuses in MANAGER_GROUPS.items()
        }

    async def list_tickets(self, group: str, page: int) -> tuple[list[TicketRow], int]:
        offset = max(page, 0) * PAGE_SIZE
        scope: dict = {}
        newest_first = True
        if self.actor.role is Role.MANAGER and group in MANAGER_GROUPS:
            statuses = MANAGER_GROUPS[group]
        elif self.actor.role is Role.CUSTOMER and group == "mine":
            statuses, scope = tuple(S), {"customer_id": self.actor.id}
        elif self.actor.role is Role.TECHNICIAN and group == "tasks":
            statuses, scope, newest_first = (
                TECHNICIAN_TASK_STATUSES, {"technician_id": self.actor.id}, False,
            )
        else:
            raise PermissionDenied()
        rows = await self.tickets.list_rows(
            self.company.id, statuses, offset=offset, limit=PAGE_SIZE,
            newest_first=newest_first, **scope,
        )
        total = await self.tickets.count(self.company.id, statuses, **scope)
        return [TicketRow(*row) for row in rows], total

    async def get_details(self, ticket_id: int) -> TicketDetails:
        ticket = await self._visible(ticket_id)
        elevator = await self.elevators.get(self.company.id, ticket.elevator_id)
        building = await self.buildings.get(self.company.id, elevator.building_id)
        customer = await self.users.get_by_id(self.company.id, ticket.customer_id)
        technician = (
            await self.users.get_by_id(self.company.id, ticket.technician_id)
            if ticket.technician_id else None
        )
        photos = await self.tickets.list_attachments(self.company.id, ticket.id)
        reports: list[TicketReport] = []
        if self.actor.role is not Role.CUSTOMER:  # customers never see technician reports
            reports = await self.tickets.list_reports(self.company.id, ticket.id)
            if self.actor.role is Role.TECHNICIAN:
                reports = [r for r in reports if r.technician_id == self.actor.id]
        feedback = None
        if self.actor.role is not Role.TECHNICIAN:
            feedback = await self.tickets.get_feedback(self.company.id, ticket.id)
        return TicketDetails(ticket, elevator, building, customer, technician, photos, reports, feedback)

    async def active_technicians(self) -> list[User]:
        self._manager_only()
        return await self.users.list_active_by_role(self.company.id, Role.TECHNICIAN)

    # ---- manager actions ----

    async def assign(self, ticket_id: int, technician_id: int) -> Ticket:
        """Assign or change the technician (before the work is reported)."""
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, S.NEW, S.ASSIGNED, S.IN_PROGRESS)
        tech = await self.users.get_by_id(self.company.id, technician_id)
        if tech is None or tech.role is not Role.TECHNICIAN or not tech.is_active:
            raise InvalidInput("err_technician_invalid")
        if ticket.technician_id == tech.id:
            raise InvalidInput("err_same_technician")
        previous = ticket.technician_id
        ticket.technician_id = tech.id
        ticket.assigned_at = _now()
        ticket.started_at = None  # the new technician starts again
        await self._move(ticket, S.ASSIGNED, note="reassigned" if previous else None)
        await self._log("ticket_assigned", ticket, technician_id=tech.id, previous_technician_id=previous)
        building, elevator = await self._label(ticket)
        self._emit(tech, "notif_assigned", ticket.id, building=building, elevator=elevator)
        return ticket

    async def approve_report(self, ticket_id: int) -> Ticket:
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, S.REPORTED)
        report = await self._latest_report(ticket)
        if report.review_status is not ReviewStatus.PENDING:
            raise InvalidInput("err_ticket_state")
        self._review(report, ReviewStatus.APPROVED)
        await self._log("report_approved", ticket)
        return ticket

    async def reject_report(self, ticket_id: int, note: str) -> Ticket:
        """Send the ticket back to the technician with the manager's note."""
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, S.REPORTED)
        clean_note = validate_field("note", note)
        report = await self._latest_report(ticket)
        self._review(report, ReviewStatus.REJECTED, clean_note)
        await self._move(ticket, S.IN_PROGRESS, note=clean_note)
        await self._log("report_rejected", ticket)
        technician = await self.users.get_by_id(self.company.id, ticket.technician_id)
        self._emit(technician, "notif_report_rejected", ticket.id, note=clean_note)
        return ticket

    async def close(self, ticket_id: int) -> Ticket:
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, S.REPORTED)
        if (await self._latest_report(ticket)).review_status is not ReviewStatus.APPROVED:
            raise InvalidInput("err_report_not_approved")
        ticket.closed_at, ticket.closed_by_id = _now(), self.actor.id
        await self._move(ticket, S.CLOSED)
        await self._log("ticket_closed", ticket)
        return ticket

    async def cancel(self, ticket_id: int) -> Ticket:
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, *OPEN_STATUSES)
        await self._move(ticket, S.CANCELLED)
        await self._log("ticket_cancelled", ticket)
        return ticket

    # ---- technician actions ----

    async def start_work(self, ticket_id: int) -> Ticket:
        ticket = await self._own_task(ticket_id)
        self._require_status(ticket, S.ASSIGNED)
        ticket.started_at = _now()
        await self._move(ticket, S.IN_PROGRESS)
        return ticket

    async def submit_report(self, ticket_id: int, text: str) -> Ticket:
        ticket = await self._own_task(ticket_id)
        self._require_status(ticket, S.IN_PROGRESS)
        clean = validate_field("report", text)
        await self.tickets.add_report(TicketReport(
            company_id=self.company.id, ticket_id=ticket.id, technician_id=self.actor.id,
            text=clean, review_status=ReviewStatus.PENDING,
        ))
        ticket.reported_at = _now()
        await self._move(ticket, S.REPORTED)
        await self._notify_managers("notif_report_submitted", ticket)
        return ticket

    # ---- feedback (FR-6): optional, never required to close ----

    async def check_feedback_allowed(self, ticket_id: int) -> Ticket:
        """The ticket's customer may leave one piece of feedback once the work is reported."""
        require_role(self.actor, Role.CUSTOMER)
        ticket = await self._visible(ticket_id)
        self._require_status(ticket, *FEEDBACK_STATUSES)
        if await self.tickets.get_feedback(self.company.id, ticket.id) is not None:
            raise InvalidInput("err_feedback_exists")
        return ticket

    async def submit_feedback(self, ticket_id: int, rating: int | str, comment: str | None = None):
        score = validate_rating(rating)
        text = validate_field("comment", comment)
        ticket = await self.check_feedback_allowed(ticket_id)
        feedback = await self.tickets.add_feedback(TicketFeedback(
            company_id=self.company.id, ticket_id=ticket.id, rating=score, comment=text,
        ))
        await self._log("feedback_submitted", ticket, rating=score)
        await self._notify_managers("notif_feedback_received", ticket, rating=score)
        return feedback

    async def request_feedback(self, ticket_id: int) -> Ticket:
        """Ask the customer for feedback (sends them a notification)."""
        self._manager_only()
        ticket = await self._get(ticket_id)
        self._require_status(ticket, *FEEDBACK_STATUSES)
        if await self.tickets.get_feedback(self.company.id, ticket.id) is not None:
            raise InvalidInput("err_feedback_exists")
        customer = await self.users.get_by_id(self.company.id, ticket.customer_id)
        if not customer.telegram_id:
            raise InvalidInput("err_customer_not_linked")
        self._emit(customer, "notif_feedback_requested", ticket.id, action="feedback")
        await self._log("feedback_requested", ticket)
        return ticket

    # ---- helpers ----

    def _manager_only(self) -> None:
        require_role(self.actor, Role.MANAGER)

    async def _reporting_customer_id(self, customer_id: int | None) -> int:
        if self.actor.role is Role.CUSTOMER:
            return self.actor.id
        if self.actor.role is not Role.MANAGER:
            raise PermissionDenied()
        customer = await self.users.get_by_id(self.company.id, customer_id) if customer_id else None
        if customer is None or customer.role is not Role.CUSTOMER:
            raise NotFound()
        if not customer.is_active:
            raise InvalidInput("err_inactive_parent")
        return customer.id

    async def _get(self, ticket_id: int) -> Ticket:
        ticket = await self.tickets.get(self.company.id, ticket_id)
        if ticket is None:
            raise NotFound()
        return ticket

    async def _visible(self, ticket_id: int) -> Ticket:
        """Managers see all tickets; customers their own; technicians those assigned to them."""
        ticket = await self._get(ticket_id)
        role = self.actor.role
        if role is Role.MANAGER:
            return ticket
        if role is Role.CUSTOMER and ticket.customer_id == self.actor.id:
            return ticket
        if role is Role.TECHNICIAN and ticket.technician_id == self.actor.id:
            return ticket
        raise NotFound()  # do not reveal that the ticket exists

    async def _own_task(self, ticket_id: int) -> Ticket:
        require_role(self.actor, Role.TECHNICIAN)
        ticket = await self._get(ticket_id)
        if ticket.technician_id != self.actor.id:
            raise NotFound()
        return ticket

    async def _latest_report(self, ticket: Ticket) -> TicketReport:
        reports = await self.tickets.list_reports(self.company.id, ticket.id)
        if not reports:
            raise InvalidInput("err_ticket_state")
        return reports[-1]

    def _review(self, report: TicketReport, status: ReviewStatus, note: str | None = None) -> None:
        report.review_status = status
        report.review_note = note
        report.reviewed_by_id = self.actor.id
        report.reviewed_at = _now()

    @staticmethod
    def _require_status(ticket: Ticket, *allowed: TicketStatus) -> None:
        if ticket.status not in allowed:
            raise InvalidInput("err_ticket_state")

    async def _move(self, ticket: Ticket, new_status: TicketStatus, note: str | None = None) -> None:
        old_status = ticket.status
        await self.tickets.add_history(
            self.company.id, ticket.id, old_status, new_status, self.actor.id, note
        )
        ticket.status = new_status
        if new_status is not old_status:  # the customer follows the progress (no report details)
            customer = await self.users.get_by_id(self.company.id, ticket.customer_id)
            self._emit(customer, f"notif_status_{new_status.value}", ticket.id)

    async def _log(self, event_type: str, ticket: Ticket, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="ticket",
            entity_id=ticket.id, details=details,
        )

    # ---- notifications ----

    def _emit(self, user: User, key: str, ticket_id: int, action: str = "view", **params) -> None:
        """Queue a notification; users who never signed in have nowhere to receive it."""
        box = self.outbox or current_outbox.get()
        if box is None or not user.telegram_id:
            return
        box.add(Notification(
            telegram_id=user.telegram_id,
            language=user.language or self.company.default_language,
            key=key, params={"id": ticket_id, **params}, ticket_id=ticket_id, action=action,
        ))

    async def _notify_managers(self, key: str, ticket: Ticket, **params) -> None:
        for manager in await self.users.list_active_by_role(self.company.id, Role.MANAGER):
            if manager.id != self.actor.id:
                self._emit(manager, key, ticket.id, **params)

    async def _label(self, ticket: Ticket) -> tuple[str, str]:
        elevator = await self.elevators.get(self.company.id, ticket.elevator_id)
        building = await self.buildings.get(self.company.id, elevator.building_id)
        return building.name, elevator.name
