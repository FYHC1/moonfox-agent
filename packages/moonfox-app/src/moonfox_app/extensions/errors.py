"""Failures at the explicit extension-hosting boundary."""


class ExtensionError(Exception):
    """Base class for infrastructure errors (plugin exceptions keep their identity)."""


class ManifestError(ExtensionError, ValueError):
    """Invalid identity, version, scope, or capability declaration."""


class DependencyError(ExtensionError, ValueError):
    """Missing, disabled, cyclic, or scope-incompatible dependency."""


class CapabilityError(ExtensionError, PermissionError):
    """A capability was not requested, granted, or available in this scope."""


class RegistrationError(ExtensionError, ValueError):
    """Invalid or conflicting contribution."""


class NamespaceError(RegistrationError):
    """An extension attempted to contribute outside its own namespace."""


class LifecycleError(ExtensionError, RuntimeError):
    """An operation is unavailable in the current lifecycle phase."""


class ExtensionRevokedError(LifecycleError):
    """An API belonging to a previous/disabled instance was used."""


class CommandAuthorizationError(ExtensionError, PermissionError):
    """The mandatory command authorizer did not explicitly allow execution."""


class CommandNotFoundError(ExtensionError, LookupError):
    """No active extension owns this command."""


def describe(exc: BaseException) -> str:
    """Diagnostic rendering must not replace the failure if a plugin has a broken __str__."""
    try:
        detail = str(exc)
    except BaseException:
        detail = "<unprintable exception>"
    return f"{type(exc).__name__}: {detail}"[:2048]
