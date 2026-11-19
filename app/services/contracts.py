"""Service contracts of buildings (FR-13): managers manage them, the owning customer sees them
and may ask for a renewal. Expiry reminders are in contract_reminders.py."""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.core.timeutil import local_today
from app.models import Building, Company, Contract, ContractStatus, Role, User
from app.repositories.audit import AuditRepository
from app.repositories.buildings import BuildingRepository
from app.repositories.contracts import ContractRepository
from app.repositories.users import UserRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from app.services.permissions import require_role
from app.services.validation import validate_field

log = logging.getLogger(__name__)

GROUPS = ("expiring", "active", "ended")
RENEWAL_COOLDOWN = timedelta(hours=24)  # one renewal request per contract per day
_EDITABLE = {"end_date", "contract_notes"}


@dataclass
class ContractDetails:
    contract: Contract
    building: Building
    owner: User


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContractService:
    def __init__(
        self, session: AsyncSession, company: Company, actor: User | None,
        outbox: Outbox | None = None,
    ):
        self.actor = require_role(actor, Role.MANAGER, Role.CUSTOMER)
        self.company = company
        self.outbox = outbox  # tests pass one; the bot uses the per-update outbox
        self.contracts = ContractRepository(session)
        self.buildings = BuildingRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    @property
    def _is_manager(self) -> bool:
        return self.actor.role is Role.MANAGER

    def _manager_only(self) -> None:
        if not self._is_manager:
            raise PermissionDenied()

    def require_manager(self) -> None:
        """For screens that only start a manager action (the action itself is checked again)."""
        self._manager_only()

    # ---- reading ----

    async def counts(self, today: date | None = None) -> dict[str, int]:
        """Manager home: number of contracts per group."""
        self._manager_only()
        today = today or local_today()
        return {g: await self.contracts.count_group(self.company.id, g, today) for g in GROUPS}

    async def list_contracts(
        self, group: str, page: int, today: date | None = None
    ) -> tuple[list[tuple[Contract, Building]], int]:
        """Managers: group expiring / active / ended. Customers: group "mine" (own buildings)."""
        today = today or local_today()
        offset = max(page, 0) * PAGE_SIZE
        if self._is_manager:
            if group not in GROUPS:
                raise InvalidInput()
            owner_id = None
        else:
            owner_id = self.actor.id
        rows = await self.contracts.list_group(
            self.company.id, group, today, offset, PAGE_SIZE, owner_id=owner_id
        )
        return rows, await self.contracts.count_group(self.company.id, group, today, owner_id)

    async def list_for_building(self, building_id: int) -> tuple[Building, list[Contract]]:
        self._manager_only()
        building = await self._building(building_id)
        return building, await self.contracts.list_by_building(self.company.id, building_id)

    async def get_details(self, contract_id: int) -> ContractDetails:
        contract = await self._get(contract_id)
        building = await self._building(contract.building_id)
        if not self._is_manager and building.owner_id != self.actor.id:
            raise NotFound()  # customers only see their own contracts
        owner = await self.users.get_by_id(self.company.id, building.owner_id)
        return ContractDetails(contract, building, owner)

    # ---- manager actions ----

    async def create(
        self, building_id: int, start_raw: str, end_raw: str, notes: str | None = None
    ) -> Contract:
        self._manager_only()
        building = await self._building(building_id)
        if not building.is_active:
            raise InvalidInput("err_inactive_parent")
        start = validate_field("start_date", start_raw)
        end = validate_field("end_date", end_raw)
        clean_notes = validate_field("contract_notes", notes)
        await self._check_period(building.id, start, end)

        contract = await self.contracts.add(Contract(
            company_id=self.company.id, building_id=building.id, start_date=start, end_date=end,
            status=ContractStatus.ACTIVE, notes=clean_notes,
        ))
        await self._log("contract_created", contract, building_id=building.id)
        return contract

    async def update_field(self, contract_id: int, field: str, raw: str | None) -> Contract:
        self._manager_only()
        if field not in _EDITABLE:
            raise InvalidInput()
        contract = await self._get(contract_id)
        if contract.status is not ContractStatus.ACTIVE:
            raise InvalidInput("err_contract_state")
        value = validate_field(field, raw)
        if field == "end_date":
            await self._check_period(contract.building_id, contract.start_date, value, contract.id)
            contract.end_date = value
            # a new period: the expiry reminders start over and the old renewal request is settled
            await self.contracts.clear_reminders(contract.id)
            contract.renewal_requested_at = None
        else:
            contract.notes = value
        await self._log("contract_updated", contract, field=field)
        return contract

    async def cancel(self, contract_id: int) -> Contract:
        self._manager_only()
        contract = await self._get(contract_id)
        if contract.status is not ContractStatus.ACTIVE:
            raise InvalidInput("err_contract_state")
        contract.status = ContractStatus.CANCELLED
        await self._log("contract_cancelled", contract)
        return contract

    # ---- customer action ----

    async def request_renewal(self, contract_id: int, today: date | None = None) -> Contract:
        """The owning customer asks the company to renew; all managers are notified."""
        if self.actor.role is not Role.CUSTOMER:
            raise PermissionDenied()
        details = await self.get_details(contract_id)  # also checks ownership
        contract = details.contract
        today = today or local_today()
        if contract.status is not ContractStatus.ACTIVE or contract.end_date < today:
            raise InvalidInput("err_contract_state")
        last = contract.renewal_requested_at
        if last is not None and _now() - last < RENEWAL_COOLDOWN:
            raise Conflict("err_renewal_recent")
        contract.renewal_requested_at = _now()
        await self._log("contract_renewal_requested", contract)

        box = self.outbox or current_outbox.get()
        if box is not None:
            for manager in await self.users.list_active_by_role(self.company.id, Role.MANAGER):
                if manager.telegram_id:
                    box.add(Notification(
                        telegram_id=manager.telegram_id,
                        language=manager.language or self.company.default_language,
                        key="notif_renewal_requested",
                        params={
                            "id": contract.id, "customer": self.actor.name,
                            "building": details.building.name, "end": contract.end_date.isoformat(),
                        },
                        contract_id=contract.id,
                    ))
        return contract

    # ---- helpers ----

    async def _get(self, contract_id: int) -> Contract:
        contract = await self.contracts.get(self.company.id, contract_id)
        if contract is None:
            raise NotFound()
        return contract

    async def _building(self, building_id: int) -> Building:
        building = await self.buildings.get(self.company.id, building_id)
        if building is None:
            raise NotFound()
        return building

    async def _check_period(
        self, building_id: int, start: date, end: date, exclude_id: int | None = None
    ) -> None:
        if end < start:
            raise InvalidInput("err_contract_dates")
        if await self.contracts.overlapping(self.company.id, building_id, start, end, exclude_id):
            raise Conflict("err_contract_overlap")

    async def _log(self, event_type: str, contract: Contract, **details) -> None:
        await self.audit.add(
            self.company.id, event_type, actor_id=self.actor.id, entity_type="contract",
            entity_id=contract.id, details=details,
        )
