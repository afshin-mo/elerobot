from app.models import Role, User
from app.services.errors import PermissionDenied


def require_role(user: User | None, *roles: Role) -> User:
    """Raise PermissionDenied unless the user is active and has one of the roles."""
    if user is None or not user.is_active or user.role not in roles:
        raise PermissionDenied()
    return user
