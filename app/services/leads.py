"""Consultation requests from guests (FR-10) and their handling by managers (FR-11).

Guests are not signed in, so GuestLeadService has no actor. The phone number must come from
Telegram's contact button; the bot checks that it belongs to the sender before calling here.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import Notification, Outbox, current_outbox
from app.core.phone import normalize_phone
from app.models import Company, Lead, LeadStatus, Role, User
from app.repositories.audit import AuditRepository
from app.repositories.leads import LeadRepository
from app.repositories.users import UserRepository
from app.services.directory import PAGE_SIZE
from app.services.errors import Conflict, InvalidInput, NotFound
from app.services.permissions import require_role
from app.services.validation import validate_field

log = logging.getLogger(__name__)

MAX_LEADS_PER_DAY = 3  # per Telegram account, rolling 24 hours (spam protection)
_ALLOWED_MOVES = {
    LeadStatus.NEW: {LeadStatus.CONTACTED, LeadStatus.CLOSED},
    LeadStatus.CONTACTED: {LeadStatus.CLOSED},
    LeadStatus.CLOSED: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GuestLeadService:
    def __init__(self, session: AsyncSession, company: Company, outbox: Outbox | None = None):
        self.company = company
        self.outbox = outbox  # tests pass one; the bot uses the per-update outbox
        self.leads = LeadRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    async def check_allowed(self, telegram_id: int) -> None:
        """Raise Conflict when the sender has used up today's requests."""
        since = _now() - timedelta(days=1)
        if await self.leads.count_since(self.company.id, telegram_id, since) >= MAX_LEADS_PER_DAY:
            raise Conflict("err_lead_limit", max=MAX_LEADS_PER_DAY)

    async def submit(
        self, telegram_id: int, username: str | None, name: str, raw_phone: str, message: str
    ) -> Lead:
        phone = normalize_phone(raw_phone)
        if phone is None:
            raise InvalidInput("invalid_phone")
        text = validate_field("lead_message", message)
        await self.check_allowed(telegram_id)

        lead = await self.leads.add(Lead(
            company_id=self.company.id, telegram_id=telegram_id,
            telegram_username=username[:64] if username else None,
            name=(name or "").strip()[:200] or "-", phone=phone, message=text,
            status=LeadStatus.NEW,
        ))
        # the audit log keeps only the last 4 digits of phone numbers
        await self.audit.add(
            self.company.id, "lead_created", entity_type="lead", entity_id=lead.id,
            details={"phone_last4": phone[-4:]},
        )
        await self._notify_managers(lead)
        return lead

    async def _notify_managers(self, lead: Lead) -> None:
        box = self.outbox or current_outbox.get()
        if box is None:
            return
        for manager in await self.users.list_active_by_role(self.company.id, Role.MANAGER):
            if not manager.telegram_id:
                continue
            box.add(Notification(
                telegram_id=manager.telegram_id,
                language=manager.language or self.company.default_language,
                key="notif_new_lead",
                params={
                    "id": lead.id, "name": lead.name, "phone": lead.phone,
                    "username": f"@{lead.telegram_username}" if lead.telegram_username else "-",
                    "message": lead.message[:500],
                },
                lead_id=lead.id,
            ))


class LeadService:
    """Manager side: list requests and mark them handled."""

    def __init__(self, session: AsyncSession, company: Company, actor: User | None):
        self.actor = require_role(actor, Role.MANAGER)
        self.company = company
        self.leads = LeadRepository(session)
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    async def counts(self) -> tuple[int, int]:
        """(all requests, new requests)"""
        return (
            await self.leads.count(self.company.id),
            await self.leads.count(self.company.id, LeadStatus.NEW),
        )

    async def list_leads(self, page: int) -> tuple[list[Lead], int, int]:
        """(requests on the page, total, number of new ones)"""
        total, new = await self.counts()
        rows = await self.leads.list_page(self.company.id, max(page, 0) * PAGE_SIZE, PAGE_SIZE)
        return rows, total, new

    async def get(self, lead_id: int) -> tuple[Lead, User | None]:
        """The request and the manager who handled it (None while new)."""
        lead = await self.leads.get(self.company.id, lead_id)
        if lead is None:
            raise NotFound()
        handler = None
        if lead.handled_by_id is not None:
            handler = await self.users.get_by_id(self.company.id, lead.handled_by_id)
        return lead, handler

    async def set_status(self, lead_id: int, new_status: LeadStatus) -> Lead:
        lead = await self.leads.get(self.company.id, lead_id)
        if lead is None:
            raise NotFound()
        if new_status not in _ALLOWED_MOVES[lead.status]:
            raise InvalidInput("err_lead_state")
        old = lead.status
        lead.status = new_status
        lead.handled_by_id = self.actor.id
        lead.handled_at = _now()
        await self.audit.add(
            self.company.id, "lead_status_changed", actor_id=self.actor.id,
            entity_type="lead", entity_id=lead.id,
            details={"from": old.value, "to": new_status.value},
        )
        return lead
