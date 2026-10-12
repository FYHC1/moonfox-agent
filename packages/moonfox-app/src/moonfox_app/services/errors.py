"""Service failures mapped to transport responses by adapters."""


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ResyncRequired(RuntimeError):
    pass


class InvalidConfiguration(ValueError):
    pass
