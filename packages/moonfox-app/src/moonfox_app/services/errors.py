"""Service failures mapped to transport responses by adapters."""

from moonfox_app.extension_api import ModelRequestRejectedError


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class BudgetExceeded(ModelRequestRejectedError):
    pass


class ResyncRequired(RuntimeError):
    pass


class InvalidConfiguration(ValueError):
    pass
