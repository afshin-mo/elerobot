import pytest

from app.models import AuditLog, Company, Role, User
from app.services.auth import AuthService, LoginStatus
from app.services.errors import InvalidInput, PermissionDenied
from app.services.permissions import require_role
from sqlalchemy import select

PHONE = "+9647501234567"


async def _company(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    return company


async def _user(session, company, role=Role.CUSTOMER, phone=PHONE, **kw):
    user = User(company_id=company.id, phone=phone, name="Ali", role=role, **kw)
    session.add(user)
    await session.flush()
    return user


def _auth(session, company, manager_id=None):
    return AuthService(session, company, manager_id)


async def _events(session, company):
    rows = await session.execute(select(AuditLog.event_type).where(AuditLog.company_id == company.id))
    return list(rows.scalars())


async def test_registered_user_links_telegram_account(session):
    company = await _company(session)
    user = await _user(session, company)
    result = await _auth(session, company).login(111, "9647501234567", "Ali T")
    assert result.status is LoginStatus.OK and result.user.id == user.id
    assert user.telegram_id == 111
    assert await _events(session, company) == ["telegram_linked"]


async def test_second_login_by_telegram_id_needs_no_phone(session):
    company = await _company(session)
    await _user(session, company, telegram_id=111)
    result = await _auth(session, company).login(111, "garbage", "Ali")
    assert result.status is LoginStatus.OK


async def test_unregistered_phone_is_denied(session):
    company = await _company(session)
    result = await _auth(session, company).login(111, "+9647509999999", "X")
    assert result.status is LoginStatus.NOT_REGISTERED and result.user is None


async def test_invalid_phone(session):
    company = await _company(session)
    result = await _auth(session, company).login(111, "12", "X")
    assert result.status is LoginStatus.INVALID_PHONE


async def test_inactive_user_is_denied(session):
    company = await _company(session)
    await _user(session, company, is_active=False)
    result = await _auth(session, company).login(111, PHONE, "Ali")
    assert result.status is LoginStatus.INACTIVE


async def test_phone_already_linked_to_other_telegram_account(session):
    company = await _company(session)
    await _user(session, company, telegram_id=222)
    result = await _auth(session, company).login(111, PHONE, "Ali")
    assert result.status is LoginStatus.PHONE_LINKED_TO_OTHER


async def test_phone_of_another_company_is_not_visible(session):
    company = await _company(session)
    other = await _company(session)
    await _user(session, other)
    result = await _auth(session, company).login(111, PHONE, "Ali")
    assert result.status is LoginStatus.NOT_REGISTERED


async def test_first_manager_bootstrap_from_env_id(session):
    company = await _company(session)
    result = await _auth(session, company, manager_id=555).login(555, PHONE, "Boss")
    assert result.status is LoginStatus.OK
    assert result.user.role is Role.MANAGER and result.user.telegram_id == 555
    assert result.user.is_admin is True  # the configured first manager is the admin
    assert await _events(session, company) == ["first_manager_created"]


async def test_bootstrap_only_for_env_id(session):
    company = await _company(session)
    result = await _auth(session, company, manager_id=555).login(556, PHONE, "Other")
    assert result.status is LoginStatus.NOT_REGISTERED


async def test_bootstrap_not_repeated_when_a_manager_exists(session):
    company = await _company(session)
    await _user(session, company, role=Role.MANAGER, phone="+9647500000009")
    result = await _auth(session, company, manager_id=555).login(555, PHONE, "Boss")
    assert result.status is LoginStatus.NOT_REGISTERED


async def test_set_language_validates(session):
    company = await _company(session)
    user = await _user(session, company)
    auth = _auth(session, company)
    await auth.set_language(user, "fa")
    assert user.language == "fa"
    with pytest.raises(InvalidInput):
        await auth.set_language(user, "xx")


async def test_require_role(session):
    company = await _company(session)
    manager = await _user(session, company, role=Role.MANAGER)
    customer = await _user(session, company, phone="+9647500000002")
    assert require_role(manager, Role.MANAGER) is manager
    with pytest.raises(PermissionDenied):
        require_role(customer, Role.MANAGER)
    with pytest.raises(PermissionDenied):
        require_role(None, Role.MANAGER)
    manager.is_active = False
    with pytest.raises(PermissionDenied):
        require_role(manager, Role.MANAGER)


async def test_ensure_initial_admin_promotes_only_the_configured_active_manager(session):
    company = await _company(session)
    boss = await _user(session, company, Role.MANAGER, "+9647500000020", telegram_id=555)
    other = await _user(session, company, Role.MANAGER, "+9647500000021", telegram_id=556)
    assert await _auth(session, company, manager_id=555).ensure_initial_admin() is True
    assert boss.is_admin and not other.is_admin
    assert await _auth(session, company, manager_id=555).ensure_initial_admin() is False  # idempotent
    assert await _events(session, company) == ["admin_granted"]


async def test_ensure_initial_admin_ignores_other_cases(session):
    company = await _company(session)
    customer = await _user(session, company, Role.CUSTOMER, "+9647500000022", telegram_id=600)
    inactive = await _user(session, company, Role.MANAGER, "+9647500000023", telegram_id=601, is_active=False)
    assert await _auth(session, company, manager_id=None).ensure_initial_admin() is False
    assert await _auth(session, company, manager_id=999).ensure_initial_admin() is False  # unknown id
    assert await _auth(session, company, manager_id=600).ensure_initial_admin() is False  # not a manager
    assert await _auth(session, company, manager_id=601).ensure_initial_admin() is False  # inactive
    assert not customer.is_admin and not inactive.is_admin
