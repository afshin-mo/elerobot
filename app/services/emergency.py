"""QR emergency alert (FR-12, D-014).

Anyone who scans an elevator's QR code, signed in or not, sends the managers an alert with the
building and elevator details. The link carries a random token (never the elevator id). Alerts
are limited per elevator and per Telegram account. An alert becomes a ticket only after a
manager confirms; the ticket's customer is the building's owner. No location, no customer data
in the alert.
"""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.core.timeutil import LOCAL_TZ
from app.models import (
    AlertStatus, Building, Company, Elevator, EmergencyAlert, Role, Ticket, User,
)
from app.repositories.alerts import AlertRepository
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.elevators import ElevatorRepository
from app.repositories.users import UserRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import InvalidInput, NotFound
from app.services.permissions import require_role
from app.services.tickets import TicketService

log = logging.getLogger(__name__)

PAYLOAD_PREFIX = "unit_"
ELEVATOR_WINDOW = timedelta(minutes=10)  # one alert per elevator in this time
ACCOUNT_LIMIT = 5  # alerts per Telegram account ...
ACCOUNT_WINDOW = timedelta(hours=1)  # ... in this time
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,32}")


def new_token() -> str:
    return secrets.token_urlsafe(16)  # 128 random bits, 22 characters


def payload_for(token: str) -> str:
    return PAYLOAD_PREFIX + token


def token_from_payload(payload: str | None) -> str | None:
    """The token of a /start payload like "unit_<token>"; None for anything else."""
    if not payload or not payload.startswith(PAYLOAD_PREFIX):
        return None
    token = payload[len(PAYLOAD_PREFIX):]
    return token if _TOKEN.fullmatch(token) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    elevator: Elevator
    building: Building
    state: str  # "sent" | "recent" (managers were told a moment ago) | "limited" | "inactive"
    alert: EmergencyAlert | None = None


class EmergencyAccessService:
    """The guest side: no actor, only the token from the QR code."""

    def __init__(self, session: AsyncSession, company: Company, outbox: Outbox | None = None):
        self.company = company
        self.outbox = outbox
        self.elevators = ElevatorRepository(session)
        self.buildings = BuildingRepository(session)
        self.alerts = AlertRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    async def lookup(self, token: str) -> tuple[Elevator, Building] | None:
        elevator = await self.elevators.get_by_token(self.company.id, token)
        if elevator is None:
            return None
        building = await self.buildings.get(self.company.id, elevator.building_id)
        return (elevator, building) if building is not None else None

    async def trigger(
        self, token: str, telegram_id: int, username: str | None, name: str
    ) -> Outcome | None:
        """Scan: create the alert and tell the managers. None if the token is unknown."""
        found = await self.lookup(token)
        if found is None:
            return None
        elevator, building = found
        if not elevator.is_active or not building.is_active:
            return Outcome(elevator, building, "inactive")
        now = _now()
        if await self.alerts.count_for_elevator_since(self.company.id, elevator.id, now - ELEVATOR_WINDOW):
            return Outcome(elevator, building, "recent")
        if await self.alerts.count_for_account_since(self.company.id, telegram_id, now - ACCOUNT_WINDOW) >= ACCOUNT_LIMIT:
            return Outcome(elevator, building, "limited")

        alert = await self.alerts.add(EmergencyAlert(
            company_id=self.company.id, elevator_id=elevator.id, telegram_id=telegram_id,
            telegram_username=username[:64] if username else None,
            name=(name or "").strip()[:200] or "-", status=AlertStatus.NEW,
        ))
        await self.audit.add(
            self.company.id, "emergency_alert_created", entity_type="emergency_alert",
            entity_id=alert.id, details={"elevator_id": elevator.id, "telegram_id": telegram_id},
        )
        await self._notify_managers(alert, elevator, building)
        return Outcome(elevator, building, "sent", alert)

    async def _notify_managers(self, alert: EmergencyAlert, elevator: Elevator, building: Building) -> None:
        box = self.outbox or current_outbox.get()
        if box is None:
            return
        when = (alert.created_at or _now()).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        for manager in await self.users.list_active_by_role(self.company.id, Role.MANAGER):
            if not manager.telegram_id:
                continue
            box.add(Notification(
                telegram_id=manager.telegram_id,
                language=manager.language or self.company.default_language,
                key="notif_emergency",
                params={
                    "id": alert.id, "building": building.name, "address": building.address or "-",
                    "elevator": elevator.name, "name": alert.name,
                    "username": f"@{alert.telegram_username}" if alert.telegram_username else "-",
                    "when": when,
                },
                alert_id=alert.id,
            ))


@dataclass
class AlertDetails:
    alert: EmergencyAlert
    elevator: Elevator
    building: Building
    owner: User | None
    handler: User | None


class AlertService:
    """The manager side: list alerts, turn one into a ticket, or dismiss it."""

    def __init__(
        self, session: AsyncSession, company: Company, actor: User | None, outbox: Outbox | None = None
    ):
        self.actor = require_role(actor, Role.MANAGER)
        self.session = session
        self.company = company
        self.outbox = outbox
        self.alerts = AlertRepository(session)
        self.elevators = ElevatorRepository(session)
        self.buildings = BuildingRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    async def list_alerts(self, page: int):
        """(rows, total, number of new alerts)"""
        rows = await self.alerts.list_page(self.company.id, max(page, 0) * PAGE_SIZE, PAGE_SIZE)
        return (
            rows, await self.alerts.count(self.company.id),
            await self.alerts.count(self.company.id, AlertStatus.NEW),
        )

    async def get_details(self, alert_id: int) -> AlertDetails:
        alert = await self.alerts.get(self.company.id, alert_id)
        if alert is None:
            raise NotFound()
        return await self._details(alert)

    async def _details(self, alert: EmergencyAlert) -> AlertDetails:
        elevator = await self.elevators.get(self.company.id, alert.elevator_id)
        building = await self.buildings.get(self.company.id, elevator.building_id)
        owner = await self.users.get_by_id(self.company.id, building.owner_id)
        handler = (
            await self.users.get_by_id(self.company.id, alert.handled_by_id) if alert.handled_by_id else None
        )
        return AlertDetails(alert, elevator, building, owner, handler)

    async def create_ticket(self, alert_id: int) -> Ticket:
        """After the manager's confirmation: a breakdown ticket for the building's owner."""
        alert = await self.alerts.get(self.company.id, alert_id, for_update=True)
        if alert is None:
            raise NotFound()
        if alert.status is not AlertStatus.NEW:
            raise InvalidInput("err_alert_state")
        details = await self._details(alert)
        ticket = await TicketService(self.session, self.company, self.actor, self.outbox).create_ticket(
            details.elevator.id,
            f"Emergency alert #{alert.id} (QR code): {details.elevator.name}, {details.building.name}",
            customer_id=details.building.owner_id,
        )
        alert.status = AlertStatus.TICKET_CREATED
        alert.ticket_id = ticket.id
        self._handled(alert)
        await self._log("emergency_ticket_created", alert, ticket_id=ticket.id)
        return ticket

    async def dismiss(self, alert_id: int) -> EmergencyAlert:
        alert = await self.alerts.get(self.company.id, alert_id, for_update=True)
        if alert is None:
            raise NotFound()
        if alert.status is not AlertStatus.NEW:
            raise InvalidInput("err_alert_state")
        alert.status = AlertStatus.DISMISSED
        self._handled(alert)
        await self._log("emergency_alert_dismissed", alert)
        return alert

    def _handled(self, alert: EmergencyAlert) -> None:
        alert.handled_by_id = self.actor.id
        alert.handled_at = _now()

    async def _log(self, event_type: str, alert: EmergencyAlert, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="emergency_alert",
            entity_id=alert.id, details=details,
        )


class QrCodeService:
    """Managers create and renew the QR token of an elevator."""

    def __init__(self, session: AsyncSession, company: Company, actor: User | None):
        self.actor = require_role(actor, Role.MANAGER)
        self.company = company
        self.elevators = ElevatorRepository(session)
        self.buildings = BuildingRepository(session)
        self.audit = AuditRepository(session)

    async def _elevator(self, elevator_id: int) -> tuple[Elevator, Building]:
        elevator = await self.elevators.get(self.company.id, elevator_id)
        if elevator is None:
            raise NotFound()
        building = await self.buildings.get(self.company.id, elevator.building_id)
        if building is None:
            raise NotFound()
        return elevator, building

    async def get_or_create(self, elevator_id: int) -> tuple[Elevator, Building, str]:
        elevator, building = await self._elevator(elevator_id)
        if elevator.qr_token is None:
            elevator.qr_token = await self._unique_token()
            await self._log("qr_created", elevator)
        return elevator, building, elevator.qr_token

    async def regenerate(self, elevator_id: int) -> tuple[Elevator, Building, str]:
        """A new token: printed codes with the old one stop working."""
        elevator, building = await self._elevator(elevator_id)
        elevator.qr_token = await self._unique_token()
        await self._log("qr_regenerated", elevator)
        return elevator, building, elevator.qr_token

    async def _unique_token(self) -> str:
        for _ in range(5):
            token = new_token()
            if await self.elevators.get_by_token(self.company.id, token) is None:
                return token
        raise RuntimeError("could not create a unique QR token")  # practically impossible

    async def _log(self, event_type: str, elevator: Elevator) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="elevator", entity_id=elevator.id,
        )
