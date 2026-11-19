class ServiceError(Exception):
    """Business-rule error. `key` is an i18n key; the bot translates it for the user."""

    default_key = "error_generic"

    def __init__(self, key: str | None = None, **params):
        self.key = key or self.default_key
        self.params = params
        super().__init__(self.key)


class PermissionDenied(ServiceError):
    default_key = "no_permission"


class NotFound(ServiceError):
    default_key = "err_not_found"


class Conflict(ServiceError):
    default_key = "err_phone_exists"


class InvalidInput(ServiceError):
    default_key = "err_invalid_text"
