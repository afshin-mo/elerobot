from app.bot.views import main_menu
from app.models import Role, User


def _buttons(user):
    _, markup = main_menu(user)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _user(role, **kw):
    return User(company_id=1, phone="+9647500000001", name="X", role=role, language="en", **kw)


def test_only_the_admin_sees_the_managers_button():
    assert "u:list:manager:0" in _buttons(_user(Role.MANAGER, is_admin=True))
    regular = _buttons(_user(Role.MANAGER, is_admin=False))
    assert "u:list:manager:0" not in regular and "t:groups" in regular and "u:list:customer:0" in regular
    assert "l:list:0" in regular  # every manager sees the consultation requests
    assert "c:menu" in regular and "v:menu" in regular and "i:menu" in regular  # ... contracts, visits, invoices


def test_menus_per_role():
    assert _buttons(_user(Role.CUSTOMER)) == ["t:new", "t:list:mine:0", "c:list:mine:0", "v:list:mine:0", "i:list:mine:0"]
    assert _buttons(_user(Role.TECHNICIAN)) == ["t:list:tasks:0"]
