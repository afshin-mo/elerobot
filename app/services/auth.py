import enum
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone import normalize_phone
from app.models import Company, Role, User
from app.repositories.audit import AuditRepository
from app.repositories.users import UserRepository
from app.services.errors import InvalidInput

log = logging.getLogger(__name__)


class LoginStatus(str, enum.Enum):
    OK = "ok"
    NOT_REGISTERED = "not_registered"
    INACTIVE = "inactive"
    INVALID_PHONE = "invalid_phone"
    PHONE_LINKED_TO_OTHER = "phone_linked_to_other"


@dataclass(frozen=True)
class LoginResult:
    status: LoginStatus
    user: User | None = None


class AuthService:
    """Login by phone number. The caller must already have verified that the phone
    belongs to the Telegram account (the bot checks contact.user_id)."""

    def __init__(
        self, session: AsyncSession, company: Company, initial_manager_telegram_id: int | None
    ):
        self.company = company
        self.initial_manager_telegram_id = initial_manager_telegram_id
        self.users = UserRepository(session)
        self.audit = AuditRepository(session)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.users.get_by_telegram_id(self.company.id, telegram_id)

    async def login(self, telegram_id: int, raw_phone: str, display_name: str) -> LoginResult:
        existing = await self.get_by_telegram_id(telegram_id)
        if existing is not None:
            ok = existing.is_active
            return LoginResult(LoginStatus.OK if ok else LoginStatus.INACTIVE, existing)

        phone = normalize_phone(raw_phone)
        if phone is None:
            return LoginResult(LoginStatus.INVALID_PHONE)

        user = await self.users.get_by_phone(self.company.id, phone)
        if user is None:
            if await self._can_bootstrap_manager(telegram_id):
                return await self._create_first_manager(telegram_id, phone, display_name)
            return LoginResult(LoginStatus.NOT_REGISTERED)
        if not user.is_active:
            return LoginResult(LoginStatus.INACTIVE)
        if user.telegram_id is not None:  # linked to a different Telegram account
            return LoginResult(LoginStatus.PHONE_LINKED_TO_OTHER)

        user.telegram_id = telegram_id
        await self.audit.add(
            self.company.id, "telegram_linked", actor_id=user.id, entity_type="user",
            entity_id=user.id,
        )
        log.info("User %s linked Telegram account", user.id)
        return LoginResult(LoginStatus.OK, user)

    async def ensure_initial_admin(self) -> bool:
        """The manager whose Telegram ID is configured in the environment is the admin.

        Idempotent; runs at startup. Admin status cannot be granted from inside the bot.
        """
        if self.initial_manager_telegram_id is None:
            return False
        user = await self.get_by_telegram_id(self.initial_manager_telegram_id)
        if user is None or user.role is not Role.MANAGER or not user.is_active or user.is_admin:
            return False
        user.is_admin = True
        await self.audit.add(
            self.company.id, "admin_granted", actor_id=user.id, entity_type="user", entity_id=user.id
        )
        log.info("User %s is now admin", user.id)
        return True

    async def set_language(self, user: User, language: str) -> None:
        if language not in self.company.active_languages:
            raise InvalidInput("err_language_not_active")
        user.language = language

    async def _can_bootstrap_manager(self, telegram_id: int) -> bool:
        return (
            self.initial_manager_telegram_id is not None
            and telegram_id == self.initial_manager_telegram_id
            and await self.users.count_managers(self.company.id) == 0
        )

    async def _create_first_manager(
        self, telegram_id: int, phone: str, display_name: str
    ) -> LoginResult:
        user = await self.users.add(
            User(
                company_id=self.company.id, phone=phone, telegram_id=telegram_id,
                name=display_name[:200] or "Manager", role=Role.MANAGER, is_admin=True,
            )
        )
        await self.audit.add(
            self.company.id, "first_manager_created", actor_id=user.id, entity_type="user",
            entity_id=user.id,
        )
        log.info("First manager created (user %s)", user.id)
        return LoginResult(LoginStatus.OK, user)
