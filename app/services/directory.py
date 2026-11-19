"""Manager-only data management: managers, customers, technicians, buildings, elevators (FR-1).

Nothing is deleted; records are deactivated. Every query is scoped by company_id.
Only the admin (a manager) manages other managers; nobody can deactivate or unlink their own account.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Building, Company, Elevator, Role, TicketStatus, User
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.elevators import ElevatorRepository
from app.repositories.tickets import TicketRepository
from app.repositories.users import UserRepository
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from app.services.permissions import require_role
from app.services.validation import FIELDS, validate_field

log = logging.getLogger(__name__)

PAGE_SIZE = 8
_TECHNICIAN_BUSY = (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.REPORTED)
_BUILDING_FIELDS = ("building_name", "address", "notes")
_ELEVATOR_FIELDS = ("elevator_name", "brand", "install_date", "notes")


def _mask(phone: str) -> str:
    return "..." + phone[-4:]


class DirectoryService:
    def __init__(self, session: AsyncSession, company: Company, actor: User | None):
        self.actor = require_role(actor, Role.MANAGER)  # fails fast for non-managers
        self.company = company
        self.users = UserRepository(session)
        self.buildings = BuildingRepository(session)
        self.elevators = ElevatorRepository(session)
        self.tickets = TicketRepository(session)
        self.audit = AuditRepository(session)

    # ---- users (managers, customers and technicians) ----

    async def list_users(self, role: Role, page: int) -> tuple[list[User], int]:
        self._check_scope(role)
        offset = max(page, 0) * PAGE_SIZE
        items = await self.users.list_by_role(self.company.id, role, offset, PAGE_SIZE)
        return items, await self.users.count_by_role(self.company.id, role)

    async def get_user(self, user_id: int) -> User:
        user = await self.users.get_by_id(self.company.id, user_id)
        if user is None:
            raise NotFound()
        self._check_scope(user.role)
        return user

    async def create_user(self, role: Role, name: str, phone: str) -> User:
        self._check_scope(role)
        clean_name = validate_field("user_name", name)
        clean_phone = validate_field("phone", phone)
        if await self.users.get_by_phone(self.company.id, clean_phone) is not None:
            raise Conflict("err_phone_exists")
        user = await self.users.add(
            User(company_id=self.company.id, phone=clean_phone, name=clean_name, role=role)
        )
        await self._log("user_created", user, role=role.value)
        return user

    async def rename_user(self, user_id: int, name: str) -> User:
        user = await self.get_user(user_id)
        user.name = validate_field("user_name", name)
        return user

    async def change_phone(self, user_id: int, phone: str) -> User:
        user = await self.get_user(user_id)
        new_phone = validate_field("phone", phone)
        other = await self.users.get_by_phone(self.company.id, new_phone)
        if other is not None and other.id != user.id:
            raise Conflict("err_phone_exists")
        old_phone, user.phone = user.phone, new_phone
        await self._log("phone_changed", user, old=_mask(old_phone), new=_mask(new_phone))
        return user

    async def set_user_active(self, user_id: int, active: bool) -> User:
        user = await self.get_user(user_id)
        self._not_self(user)
        if not active and user.role is Role.TECHNICIAN:
            busy = await self.tickets.count(
                self.company.id, _TECHNICIAN_BUSY, technician_id=user.id
            )
            if busy:
                raise Conflict("err_technician_has_tickets")
        if user.is_active != active:
            user.is_active = active
            await self._log("user_activated" if active else "user_deactivated", user)
        return user

    async def unlink_telegram(self, user_id: int) -> User:
        """Free the phone number so the person can sign in again from another account."""
        user = await self.get_user(user_id)
        self._not_self(user)
        if user.telegram_id is None:
            raise InvalidInput("err_not_linked")
        user.telegram_id = None
        await self._log("telegram_unlinked", user)
        return user

    # ---- buildings ----

    async def list_buildings(self, owner_id: int) -> list[Building]:
        owner = await self.get_user(owner_id)
        return await self.buildings.list_by_owner(self.company.id, owner.id)

    async def get_building(self, building_id: int) -> Building:
        building = await self.buildings.get(self.company.id, building_id)
        if building is None:
            raise NotFound()
        return building

    async def create_building(
        self, owner_id: int, name: str, address: str, notes: str | None = None
    ) -> Building:
        owner = await self.get_user(owner_id)
        if owner.role is not Role.CUSTOMER:
            raise NotFound()
        if not owner.is_active:
            raise InvalidInput("err_inactive_parent")
        return await self.buildings.add(
            Building(
                company_id=self.company.id, owner_id=owner.id,
                name=validate_field("building_name", name),
                address=validate_field("address", address),
                notes=validate_field("notes", notes),
            )
        )

    async def update_building(self, building_id: int, field: str, value: str | None) -> Building:
        if field not in _BUILDING_FIELDS:
            raise InvalidInput()
        building = await self.get_building(building_id)
        setattr(building, FIELDS[field].short, validate_field(field, value))
        return building

    async def set_building_active(self, building_id: int, active: bool) -> Building:
        building = await self.get_building(building_id)
        building.is_active = active
        return building

    # ---- elevators ----

    async def list_elevators(self, building_id: int) -> list[Elevator]:
        building = await self.get_building(building_id)
        return await self.elevators.list_by_building(self.company.id, building.id)

    async def get_elevator(self, elevator_id: int) -> Elevator:
        elevator = await self.elevators.get(self.company.id, elevator_id)
        if elevator is None:
            raise NotFound()
        return elevator

    async def create_elevator(
        self, building_id: int, name: str, brand: str | None = None,
        install_date: str | None = None, notes: str | None = None,
    ) -> Elevator:
        building = await self.get_building(building_id)
        if not building.is_active:
            raise InvalidInput("err_inactive_parent")
        clean_name = validate_field("elevator_name", name)
        if await self.elevators.get_by_name(self.company.id, building.id, clean_name):
            raise Conflict("err_name_exists")
        return await self.elevators.add(
            Elevator(
                company_id=self.company.id, building_id=building.id, name=clean_name,
                brand=validate_field("brand", brand),
                install_date=validate_field("install_date", install_date),
                notes=validate_field("notes", notes),
            )
        )

    async def update_elevator(self, elevator_id: int, field: str, value: str | None) -> Elevator:
        if field not in _ELEVATOR_FIELDS:
            raise InvalidInput()
        elevator = await self.get_elevator(elevator_id)
        clean = validate_field(field, value)
        if field == "elevator_name":
            other = await self.elevators.get_by_name(self.company.id, elevator.building_id, clean)
            if other is not None and other.id != elevator.id:
                raise Conflict("err_name_exists")
        setattr(elevator, FIELDS[field].short, clean)
        return elevator

    async def set_elevator_active(self, elevator_id: int, active: bool) -> Elevator:
        elevator = await self.get_elevator(elevator_id)
        elevator.is_active = active
        return elevator

    # ---- helpers ----

    def ensure_can_create(self, role: Role) -> None:
        """Early check so the bot does not start a form the actor may not finish."""
        self._check_scope(role)

    def _check_scope(self, role: Role) -> None:
        if role is Role.MANAGER and not self.actor.is_admin:
            raise PermissionDenied("err_admin_only")

    def _not_self(self, user: User) -> None:
        if user.id == self.actor.id:
            raise PermissionDenied("err_cannot_modify_self")

    async def _log(self, event_type: str, user: User, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="user",
            entity_id=user.id, details=details,
        )
