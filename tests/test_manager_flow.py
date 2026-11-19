"""Manager menus end to end: add/edit customers, buildings, elevators, unlink Telegram."""
import pytest
from aiogram import Bot
from sqlalchemy import select

from app.bot.setup import build_dispatcher
from app.core.i18n import t
from app.models import Building, Company, Elevator, Role, User
from bot_helpers import (
    TG_ID, FakeSession, Inject, button_update, contact_update, lang_update, text_update,
)


@pytest.fixture
async def env(session):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    manager = User(company_id=company.id, phone="+9647500000001", name="Boss", role=Role.MANAGER,
                   telegram_id=TG_ID, language="en", is_admin=True)
    session.add(manager)
    await session.flush()
    fake = FakeSession()
    bot = Bot("123456:TEST", session=fake)
    dp = build_dispatcher(company, None, session_middleware=Inject(session))
    counter = iter(range(1, 1000))

    async def send(update_factory, *args, **kw):
        await dp.feed_update(bot, update_factory(*args, next(counter), **kw))

    return company, session, fake, send


async def _one(session, model, **where):
    stmt = select(model).where(*[getattr(model, k) == v for k, v in where.items()])
    return (await session.execute(stmt)).scalar_one()


async def test_manager_adds_customer_building_and_elevator(env):
    company, session, fake, send = env
    await send(button_update, "u:list:customer:0")
    assert fake.sent[-1].startswith(t("en", "title_customers") + " (0)")

    await send(button_update, "u:add:customer")
    assert fake.sent[-1].startswith(t("en", "prompt_name"))
    await send(text_update, "Ali Karim")
    assert fake.sent[-1].startswith(t("en", "prompt_phone"))
    await send(text_update, "0750 123 4567")
    assert fake.sent[-1].startswith(t("en", "created"))
    customer = await _one(session, User, phone="+9647501234567")
    assert customer.role is Role.CUSTOMER and customer.name == "Ali Karim"

    await send(button_update, f"b:add:{customer.id}")
    await send(text_update, "Tower A")
    await send(text_update, "Erbil, 100m road")
    await send(text_update, "-")  # notes skipped
    building = await _one(session, Building, owner_id=customer.id)
    assert building.notes is None and fake.sent[-1].startswith(t("en", "created"))

    await send(button_update, f"e:add:{building.id}")
    await send(text_update, "E1")
    await send(text_update, "Otis")
    await send(text_update, "2023-05-31")
    await send(text_update, "-")
    elevator = await _one(session, Elevator, building_id=building.id)
    assert (elevator.name, elevator.brand) == ("E1", "Otis")
    assert str(elevator.install_date) == "2023-05-31"


async def test_invalid_input_keeps_the_step_and_shows_error(env):
    _, session, fake, send = env
    await send(button_update, "u:add:technician")
    await send(text_update, "Tek")
    await send(text_update, "123")  # invalid phone
    assert fake.sent[-1] == t("en", "invalid_phone")
    await send(text_update, "+9647502223333")  # retry the same step
    assert fake.sent[-1].startswith(t("en", "created"))
    assert (await _one(session, User, phone="+9647502223333")).role is Role.TECHNICIAN


async def test_duplicate_phone_is_reported(env):
    company, session, fake, send = env
    session.add(User(company_id=company.id, phone="+9647501234567", name="Old", role=Role.CUSTOMER))
    await session.flush()
    await send(button_update, "u:add:customer")
    await send(text_update, "New")
    await send(text_update, "07501234567")
    assert fake.sent[-1] == t("en", "err_phone_exists")


async def test_cancel_stops_a_flow(env):
    _, session, fake, send = env
    await send(button_update, "u:add:customer")
    await send(text_update, "/cancel")
    assert t("en", "cancelled") in fake.sent
    await send(text_update, "Some Name")  # no longer treated as input
    assert fake.sent[-1].startswith(t("en", "main_menu", role="Manager")[:10])


async def test_edit_deactivate_and_unlink(env):
    company, session, fake, send = env
    customer = User(company_id=company.id, phone="+9647501234567", name="Ali", role=Role.CUSTOMER,
                    telegram_id=777)
    session.add(customer)
    await session.flush()

    await send(button_update, f"u:edit:{customer.id}:user_name")
    await send(text_update, "Ali K")
    assert customer.name == "Ali K"

    await send(button_update, f"u:act:{customer.id}:0")
    assert customer.is_active is False

    await send(button_update, f"u:unlink:{customer.id}")
    assert fake.sent[-1] == t("en", "confirm_unlink", name="Ali K")
    assert customer.telegram_id == 777  # nothing changes until confirmed
    await send(button_update, f"u:unlink_ok:{customer.id}")
    assert customer.telegram_id is None


async def test_non_manager_cannot_use_manager_buttons(env):
    company, session, fake, send = env
    session.add(User(company_id=company.id, phone="+9647501234567", name="Cust", role=Role.CUSTOMER,
                     telegram_id=555, language="fa"))
    await session.flush()
    await send(button_update, "u:list:customer:0", tg_id=555)
    assert fake.sent[-1] == t("fa", "no_permission")
    await send(button_update, "u:add:customer", tg_id=555)
    assert fake.sent[-1] == t("fa", "no_permission")
    count = len((await session.execute(select(User))).scalars().all())
    assert count == 2  # manager + customer; nothing was created


async def test_admin_adds_second_manager_who_is_not_admin(env):
    company, session, fake, send = env
    admin = await _one(session, User, telegram_id=TG_ID)
    await send(button_update, "u:add:manager")
    await send(text_update, "Second Boss")
    await send(text_update, "0750 555 0003")
    assert fake.sent[-1].startswith(t("en", "created"))
    second = await _one(session, User, phone="+9647505550003")
    assert second.role is Role.MANAGER

    # the new manager signs in from another Telegram account
    await send(text_update, "/start", tg_id=888)
    await send(lang_update, "en", tg_id=888)
    await send(button_update, "g:login:en", tg_id=888)
    await send(contact_update, "9647505550003", tg_id=888)
    assert second.telegram_id == 888
    assert fake.sent[-1] == t("en", "main_menu", role="Manager")

    # a manager created from the bot is not an admin: no access to managers
    assert second.is_admin is False
    await send(button_update, "u:list:manager:0", tg_id=888)
    assert fake.sent[-1] == t("en", "err_admin_only")
    await send(button_update, "u:add:manager", tg_id=888)
    assert fake.sent[-1] == t("en", "err_admin_only")

    # the admin sees the list and can deactivate the second manager, but not themselves
    await send(button_update, "u:list:manager:0")
    assert fake.sent[-1].startswith(t("en", "title_managers") + " (2)")
    await send(button_update, f"u:act:{admin.id}:0")
    assert fake.sent[-1] == t("en", "err_cannot_modify_self")
    assert admin.is_active is True
    await send(button_update, f"u:act:{second.id}:0")
    assert second.is_active is False
    await send(button_update, "t:groups", tg_id=888)  # a deactivated manager loses access at once
    assert fake.sent[-1] == t("en", "no_permission")
