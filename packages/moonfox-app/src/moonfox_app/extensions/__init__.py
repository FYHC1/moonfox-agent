"""Explicit extension infrastructure. Importing this module never discovers or starts plugins."""

from .dispatch import Dispatcher, Handler
from .errors import (
    CapabilityError,
    CommandAuthorizationError,
    CommandNotFoundError,
    DependencyError,
    ExtensionError,
    ExtensionRevokedError,
    LifecycleError,
    ManifestError,
    NamespaceError,
    RegistrationError,
)
from .host import HostExtensionManager
from .registry import ExtensionRegistry
from .session import SessionExtensionManager

__all__ = [
    "CapabilityError",
    "CommandAuthorizationError",
    "CommandNotFoundError",
    "DependencyError",
    "Dispatcher",
    "ExtensionError",
    "ExtensionRegistry",
    "ExtensionRevokedError",
    "Handler",
    "HostExtensionManager",
    "LifecycleError",
    "ManifestError",
    "NamespaceError",
    "RegistrationError",
    "SessionExtensionManager",
]
