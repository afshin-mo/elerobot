from datetime import date

import pytest
from sqlalchemy import select

from app.models import AuditLog, Company, Role, User
from app.services.directory import PAGE_SIZE, DirectoryService
from app.services.errors import Conflict, InvalidInput, NotFound, PermissionDenied


async def _company(session, name="Test Co"):
    company = Company(name=name)
    session.add(company)
    await session.flush()
    return company


async def _user(session, company, role, phone, **kw):
    user = User(company_id=company.id, phone=phone, name=kw.pop("name", "X"), role=role, **kw)
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def ctx(session):
    company = await _company(session)
    manager = await _user(session, company, Role.MANAGER, "+9647500000001", telegram_id=1, is_admin=True)
    return session, company, manager, DirectoryService(session, company, manager)


async def _events(session, company):
    rows = await session.execute(select(AuditLog.event_type).where(AuditLog.company_id == company.id))
    return list(rows.scalars())


async def test_only_active_managers_can_use_the_service(session):
    company = await _company(session)
    customer = await _user(session, company, Role.CUSTOMER, "+9647500000002")
    tech = await _user(session, company, Role.TECHNICIAN, "+9647500000003")
    for actor in (customer, tech, None):
        with pytest.raises(PermissionDenied):
            DirectoryService(session, company, actor)
    inactive = await _user(session, company, Role.MANAGER, "+9647500000004", is_active=False)
    with pytest.raises(PermissionDenied):
        DirectoryService(session, company, inactive)


async def test_create_customer_normalizes_phone_and_logs(ctx):
    session, company, manager, svc = ctx
    user = await svc.create_user(Role.CUSTOMER, "  Ali  ", "0750 123 4567")
    assert (user.name, user.phone, user.role) == ("Ali", "+9647501234567", Role.CUSTOMER)
    assert user.telegram_id is None and user.is_active
    assert await _events(session, company) == ["user_created"]


async def test_duplicate_phone_and_bad_input_are_rejected(ctx):
    _, _, _, svc = ctx
    await svc.create_user(Role.TECHNICIAN, "T", "+9647501111111")
    with pytest.raises(Conflict):
        await svc.create_user(Role.CUSTOMER, "Other", "07501111111")
    with pytest.raises(InvalidInput):
        await svc.create_user(Role.CUSTOMER, "", "+9647502222222")
    with pytest.raises(InvalidInput):
        await svc.create_user(Role.CUSTOMER, "A", "12")


async def test_other_companies_are_untouchable(ctx):
    session, company, manager, svc = ctx
    other = await _company(session, "Other")
    foreign = await _user(session, other, Role.CUSTOMER, "+9647509999999")
    with pytest.raises(NotFound):
        await svc.get_user(foreign.id)
    with pytest.raises(NotFound):
        await svc.set_user_active(foreign.id, False)


async def test_edit_deactivate_and_unlink(ctx):
    session, company, _, svc = ctx
    user = await svc.create_user(Role.CUSTOMER, "Ali", "+9647501234567")
    await svc.rename_user(user.id, "Ali K")
    assert user.name == "Ali K"
    other = await svc.create_user(Role.CUSTOMER, "Bob", "+9647507654321")
    with pytest.raises(Conflict):
        await svc.change_phone(user.id, other.phone)
    await svc.change_phone(user.id, "+9647500000077")
    assert user.phone == "+9647500000077"

    await svc.set_user_active(user.id, False)
    assert user.is_active is False
    await svc.set_user_active(user.id, True)

    with pytest.raises(InvalidInput):  # not linked yet
        await svc.unlink_telegram(user.id)
    user.telegram_id = 555
    await svc.unlink_telegram(user.id)
    assert user.telegram_id is None
    events = await _events(session, company)
    assert {"phone_changed", "user_deactivated", "user_activated", "telegram_unlinked"} <= set(events)
    details = (await session.execute(select(AuditLog).where(AuditLog.event_type == "phone_changed"))).scalar_one().details
    assert details["new"] == "...0077" and "+964" not in str(details)  # no full numbers in the log


async def test_list_users_paginates(ctx):
    _, _, _, svc = ctx
    for i in range(PAGE_SIZE + 2):
        await svc.create_user(Role.CUSTOMER, f"C{i:02d}", f"+96475030000{i:02d}")
    await svc.create_user(Role.TECHNICIAN, "T", "+9647504000000")
    page0, total = await svc.list_users(Role.CUSTOMER, 0)
    page1, _ = await svc.list_users(Role.CUSTOMER, 1)
    assert total == PAGE_SIZE + 2 and len(page0) == PAGE_SIZE and len(page1) == 2
    assert page0[0].name == "C00"


async def test_buildings_and_elevators(ctx):
    _, _, _, svc = ctx
    customer = await svc.create_user(Role.CUSTOMER, "Ali", "+9647501234567")
    tech = await svc.create_user(Role.TECHNICIAN, "Tek", "+9647501234568")
    with pytest.raises(NotFound):  # technicians cannot own buildings
        await svc.create_building(tech.id, "B", "Addr")
    with pytest.raises(InvalidInput):
        await svc.create_building(customer.id, "B", "")  # address required

    building = await svc.create_building(customer.id, "Tower A", "Erbil, 100m road", "-")
    assert building.notes is None and building.is_active
    assert [b.id for b in await svc.list_buildings(customer.id)] == [building.id]

    lift = await svc.create_elevator(building.id, "E1", "Otis", "2023-05-31", None)
    assert lift.brand == "Otis" and lift.install_date == date(2023, 5, 31)
    with pytest.raises(Conflict):
        await svc.create_elevator(building.id, "E1")
    with pytest.raises(InvalidInput):
        await svc.create_elevator(building.id, "E2", None, "31/05/2023")

    second = await svc.create_elevator(building.id, "E2")
    with pytest.raises(Conflict):
        await svc.update_elevator(second.id, "elevator_name", "E1")
    await svc.update_elevator(second.id, "brand", "Schindler")
    await svc.update_elevator(second.id, "install_date", "-")
    assert second.brand == "Schindler" and second.install_date is None
    with pytest.raises(InvalidInput):
        await svc.update_elevator(second.id, "no_such_field", "x")

    await svc.update_building(building.id, "address", "New address")
    assert building.address == "New address"
    await svc.set_building_active(building.id, False)
    with pytest.raises(InvalidInput):  # inactive building takes no new elevators
        await svc.create_elevator(building.id, "E3")
    await svc.set_elevator_active(second.id, False)
    assert second.is_active is False
    await svc.set_user_active(customer.id, False)
    with pytest.raises(InvalidInput):  # inactive customer takes no new buildings
        await svc.create_building(customer.id, "B2", "Addr")


async def test_admin_registers_another_manager(ctx):
    session, company, manager, svc = ctx
    second = await svc.create_user(Role.MANAGER, "Second Boss", "0750 555 0001")
    assert second.role is Role.MANAGER and second.telegram_id is None
    created = (await session.execute(select(AuditLog).where(AuditLog.event_type == "user_created"))).scalar_one()
    assert created.details == {"role": "manager"} and created.actor_id == manager.id
    managers, total = await svc.list_users(Role.MANAGER, 0)
    assert total == 2 and {m.id for m in managers} == {manager.id, second.id}


async def test_nobody_can_deactivate_or_unlink_their_own_account(ctx):
    _, _, manager, svc = ctx
    with pytest.raises(PermissionDenied):
        await svc.set_user_active(manager.id, False)
    with pytest.raises(PermissionDenied):
        await svc.unlink_telegram(manager.id)
    await svc.rename_user(manager.id, "Boss Renamed")  # editing yourself is fine
    assert manager.name == "Boss Renamed" and manager.is_active and manager.telegram_id == 1


async def test_admin_manages_other_managers(ctx):
    session, company, manager, svc = ctx
    second = await svc.create_user(Role.MANAGER, "Second", "+9647505550002")
    second.telegram_id = 2
    await svc.unlink_telegram(second.id)
    assert second.telegram_id is None
    await svc.set_user_active(second.id, False)
    with pytest.raises(PermissionDenied):  # a deactivated manager loses access at once
        DirectoryService(session, company, second)
    await svc.set_user_active(second.id, True)
    assert DirectoryService(session, company, second).actor is second


async def test_regular_managers_cannot_manage_managers(ctx):
    session, company, admin, svc = ctx
    regular = await svc.create_user(Role.MANAGER, "Regular", "+9647505550010")
    regular.telegram_id = 10
    assert regular.is_admin is False  # managers created from the bot are never admins
    rsvc = DirectoryService(session, company, regular)

    with pytest.raises(PermissionDenied) as err:
        await rsvc.create_user(Role.MANAGER, "Third", "+9647505550011")
    assert err.value.key == "err_admin_only"
    for call in (rsvc.list_users(Role.MANAGER, 0), rsvc.get_user(admin.id), rsvc.get_user(regular.id),
                 rsvc.set_user_active(admin.id, False), rsvc.unlink_telegram(admin.id),
                 rsvc.rename_user(admin.id, "Hacked"), rsvc.change_phone(admin.id, "+9647509998888")):
        with pytest.raises(PermissionDenied):
            await call
    assert admin.is_active and admin.telegram_id == 1 and admin.name == "X"
    # everything else still works for a regular manager
    customer = await rsvc.create_user(Role.CUSTOMER, "Ali", "+9647505550012")
    await rsvc.set_user_active(customer.id, False)
    assert customer.is_active is False


async def test_the_admin_can_remove_a_manager_but_never_themselves(ctx):
    session, company, admin, svc = ctx
    regular = await svc.create_user(Role.MANAGER, "Regular", "+9647505550013")
    await svc.set_user_active(regular.id, False)  # "remove" = deactivate; nothing is deleted
    assert regular.is_active is False
    with pytest.raises(PermissionDenied) as err:
        await svc.set_user_active(admin.id, False)
    assert err.value.key == "err_cannot_modify_self"
    admin.telegram_id = 1
    with pytest.raises(PermissionDenied):
        await svc.unlink_telegram(admin.id)
    assert admin.is_active and admin.telegram_id == 1
