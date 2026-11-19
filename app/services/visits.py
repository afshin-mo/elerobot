"""Periodic service visits of elevators (FR-14).

Managers enter each visit by hand (date, time, optional repeat rule) and mark it done. The
building's owner (customer) sees the visits of his elevators and can confirm, cancel or ask
for another time. With a repeat rule, the next visit is created when a visit is done or
cancelled. Reminders are in visit_reminders.py.
"""
import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.core.timeutil import LOCAL_TZ
from app.models import (
    Building, Company, Elevator, RepeatUnit, Role, ServiceVisit, User, VisitStatus,
)
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.elevators import ElevatorRepository
from app.repositories.users import UserRepository
from app.repositories.visits import OPEN, VisitRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import InvalidInput, NotFound, PermissionDenied
from app.services.permissions import require_role
from app.services.validation import validate_field

log = logging.getLogger(__name__)

GROUPS = ("attention", "upcoming", "history")


@dataclass
class VisitDetails:
    visit: ServiceVisit
    elevator: Elevator
    building: Building
    owner: User


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_months(value: datetime, months: int) -> datetime:
    """Same day and time `months` later; a day that does not exist (31st) becomes the last day."""
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_time(visit: ServiceVisit, now: datetime) -> datetime | None:
    """The next occurrence of the repeat rule after `now` (late visits skip missed dates)."""
    if visit.repeat_every is None or visit.repeat_unit is None:
        return None
    base = visit.scheduled_at.astimezone(LOCAL_TZ)
    step = 1
    while True:
        if visit.repeat_unit is RepeatUnit.DAYS:
            candidate = base + timedelta(days=visit.repeat_every * step)
        else:
            candidate = add_months(base, visit.repeat_every * step)
        if candidate > now:
            return candidate
        step += 1


class VisitService:
    def __init__(
        self, session: AsyncSession, company: Company, actor: User | None,
        outbox: Outbox | None = None,
    ):
        self.actor = require_role(actor, Role.MANAGER, Role.CUSTOMER)
        self.company = company
        self.outbox = outbox  # tests pass one; the bot uses the per-update outbox
        self.visits = VisitRepository(session)
        self.elevators = ElevatorRepository(session)
        self.buildings = BuildingRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    @property
    def _is_manager(self) -> bool:
        return self.actor.role is Role.MANAGER

    def require_manager(self) -> None:
        if not self._is_manager:
            raise PermissionDenied()

    # ---- reading ----

    async def counts(self, now: datetime | None = None) -> dict[str, int]:
        self.require_manager()
        now = now or _now()
        return {g: await self.visits.count_group(self.company.id, g, now) for g in GROUPS}

    async def list_visits(self, group: str, page: int, now: datetime | None = None):
        """Managers: attention / upcoming / history. Customers: group "mine" (open visits)."""
        now = now or _now()
        offset = max(page, 0) * PAGE_SIZE
        if self._is_manager:
            if group not in GROUPS:
                raise InvalidInput()
            owner_id = None
        else:
            owner_id = self.actor.id
        rows = await self.visits.list_group(self.company.id, group, now, offset, PAGE_SIZE, owner_id)
        return rows, await self.visits.count_group(self.company.id, group, now, owner_id)

    async def list_for_elevator(self, elevator_id: int) -> tuple[Elevator, Building, list[ServiceVisit]]:
        self.require_manager()
        elevator = await self._elevator(elevator_id)
        building = await self._building(elevator.building_id)
        return elevator, building, await self.visits.list_for_elevator(self.company.id, elevator_id)

    async def get_details(self, visit_id: int) -> VisitDetails:
        visit = await self._get(visit_id)
        elevator = await self._elevator(visit.elevator_id)
        building = await self._building(elevator.building_id)
        if not self._is_manager and building.owner_id != self.actor.id:
            raise NotFound()  # customers only see the visits of their own elevators
        owner = await self.users.get_by_id(self.company.id, building.owner_id)
        return VisitDetails(visit, elevator, building, owner)

    # ---- manager actions ----

    async def create(self, elevator_id: int, time_raw: str, repeat_raw: str | None = None) -> ServiceVisit:
        self.require_manager()
        elevator = await self._elevator(elevator_id)
        building = await self._building(elevator.building_id)
        if not elevator.is_active or not building.is_active:
            raise InvalidInput("err_inactive_parent")
        when = self._future(validate_field("visit_time", time_raw))
        repeat = validate_field("visit_repeat", repeat_raw)
        visit = await self.visits.add(ServiceVisit(
            company_id=self.company.id, elevator_id=elevator.id, scheduled_at=when,
            status=VisitStatus.SCHEDULED,
            repeat_every=repeat[0] if repeat else None, repeat_unit=repeat[1] if repeat else None,
        ))
        await self._log("visit_created", visit, scheduled_at=when.isoformat())
        await self._tell_customer("notif_visit_scheduled", visit, elevator, building)
        return visit

    async def reschedule(self, visit_id: int, time_raw: str) -> ServiceVisit:
        self.require_manager()
        details = await self.get_details(visit_id)
        visit = self._open(details.visit)
        when = self._future(validate_field("visit_time", time_raw))
        visit.scheduled_at = when
        visit.status = VisitStatus.SCHEDULED  # the customer decides again
        visit.reminder_sent_at = None
        visit.customer_note = None
        await self._log("visit_rescheduled", visit, scheduled_at=when.isoformat())
        await self._tell_customer("notif_visit_moved", visit, details.elevator, details.building)
        return visit

    async def set_repeat(self, visit_id: int, repeat_raw: str | None) -> ServiceVisit:
        """Set, change or (with "-") remove the repeat rule of an open visit."""
        self.require_manager()
        visit = self._open((await self.get_details(visit_id)).visit)
        repeat = validate_field("visit_repeat", repeat_raw)
        visit.repeat_every = repeat[0] if repeat else None
        visit.repeat_unit = repeat[1] if repeat else None
        await self._log("visit_repeat_changed", visit)
        return visit

    async def mark_done(self, visit_id: int) -> tuple[ServiceVisit, ServiceVisit | None]:
        self.require_manager()
        visit = self._open((await self.get_details(visit_id)).visit)
        visit.status = VisitStatus.DONE
        visit.done_at = _now()
        await self._log("visit_done", visit)
        return visit, await self._spawn_next(visit)

    # ---- manager or the owning customer ----

    async def cancel(self, visit_id: int) -> tuple[ServiceVisit, ServiceVisit | None]:
        details = await self.get_details(visit_id)  # also checks ownership for customers
        visit = self._open(details.visit)
        if not self._is_manager and visit.scheduled_at <= _now():
            raise InvalidInput("err_visit_state")
        visit.status = VisitStatus.CANCELLED
        await self._log("visit_cancelled", visit)
        if self._is_manager:
            await self._tell_customer("notif_visit_cancelled", visit, details.elevator, details.building)
        else:
            await self._tell_managers("notif_visit_cancelled_by_customer", visit, details)
        return visit, await self._spawn_next(visit)

    # ---- customer actions ----

    async def confirm(self, visit_id: int) -> ServiceVisit:
        details = await self._customer_visit(visit_id)
        visit = details.visit
        if visit.status is not VisitStatus.SCHEDULED:
            raise InvalidInput("err_visit_state")
        visit.status = VisitStatus.CONFIRMED
        await self._log("visit_confirmed", visit)
        await self._tell_managers("notif_visit_confirmed", visit, details)
        return visit

    async def request_change(self, visit_id: int, note_raw: str | None) -> ServiceVisit:
        details = await self._customer_visit(visit_id)
        visit = details.visit
        if visit.status not in (VisitStatus.SCHEDULED, VisitStatus.CONFIRMED):
            raise InvalidInput("err_visit_state")
        visit.status = VisitStatus.CHANGE_REQUESTED
        visit.customer_note = validate_field("change_note", note_raw)
        await self._log("visit_change_requested", visit)
        await self._tell_managers("notif_visit_change_requested", visit, details)
        return visit

    # ---- helpers ----

    async def _customer_visit(self, visit_id: int) -> VisitDetails:
        if self.actor.role is not Role.CUSTOMER:
            raise PermissionDenied()
        details = await self.get_details(visit_id)
        if details.visit.scheduled_at <= _now():
            raise InvalidInput("err_visit_state")
        return details

    async def _spawn_next(self, visit: ServiceVisit) -> ServiceVisit | None:
        when = next_time(visit, _now())
        if when is None:
            return None
        elevator = await self._elevator(visit.elevator_id)
        building = await self._building(elevator.building_id)
        if not elevator.is_active or not building.is_active:
            return None
        if await self.visits.has_open_at(self.company.id, visit.elevator_id, when):
            return None
        nxt = await self.visits.add(ServiceVisit(
            company_id=self.company.id, elevator_id=visit.elevator_id, scheduled_at=when,
            status=VisitStatus.SCHEDULED, repeat_every=visit.repeat_every, repeat_unit=visit.repeat_unit,
        ))
        await self._log("visit_created", nxt, scheduled_at=when.isoformat(), repeat_of=visit.id)
        return nxt

    def _open(self, visit: ServiceVisit) -> ServiceVisit:
        if visit.status not in OPEN:
            raise InvalidInput("err_visit_state")
        return visit

    @staticmethod
    def _future(when: datetime) -> datetime:
        if when <= _now():
            raise InvalidInput("err_visit_past")
        return when

    async def _get(self, visit_id: int) -> ServiceVisit:
        visit = await self.visits.get(self.company.id, visit_id)
        if visit is None:
            raise NotFound()
        return visit

    async def _elevator(self, elevator_id: int) -> Elevator:
        elevator = await self.elevators.get(self.company.id, elevator_id)
        if elevator is None:
            raise NotFound()
        return elevator

    async def _building(self, building_id: int) -> Building:
        building = await self.buildings.get(self.company.id, building_id)
        if building is None:
            raise NotFound()
        return building

    def _params(self, visit: ServiceVisit, elevator: Elevator, building: Building, **extra) -> dict:
        when = visit.scheduled_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        return {"id": visit.id, "elevator": elevator.name, "building": building.name, "when": when, **extra}

    async def _tell_customer(self, key: str, visit: ServiceVisit, elevator: Elevator, building: Building) -> None:
        box = self.outbox or current_outbox.get()
        owner = await self.users.get_by_id(self.company.id, building.owner_id)
        if box is None or owner is None or not owner.is_active or not owner.telegram_id:
            return
        box.add(Notification(
            telegram_id=owner.telegram_id, language=owner.language or self.company.default_language,
            key=key, params=self._params(visit, elevator, building), visit_id=visit.id,
        ))

    async def _tell_managers(self, key: str, visit: ServiceVisit, details: VisitDetails) -> None:
        box = self.outbox or current_outbox.get()
        if box is None:
            return
        extra = {"customer": self.actor.name, "note": visit.customer_note or "-"}
        for manager in await self.users.list_active_by_role(self.company.id, Role.MANAGER):
            if manager.telegram_id:
                box.add(Notification(
                    telegram_id=manager.telegram_id,
                    language=manager.language or self.company.default_language, key=key,
                    params=self._params(visit, details.elevator, details.building, **extra),
                    visit_id=visit.id,
                ))

    async def _log(self, event_type: str, visit: ServiceVisit, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="service_visit",
            entity_id=visit.id, details=details,
        )
