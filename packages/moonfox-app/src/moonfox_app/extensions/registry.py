"""Explicit registration and side-effect-free enablement preflight."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from moonfox_app.extension_api import (
    SDK_VERSION,
    ExtensionManifest,
    HostExtension,
    SessionExtension,
)

from .errors import CapabilityError, DependencyError, ManifestError

SESSION_CAPABILITIES = frozenset(
    {
        "events",
        "tools",
        "prompt",
        "context",
        "commands",
        "entries",
        "enqueue",
        "boundary",
        "tool_check",
        "tasks",
        "models",
    }
)
HOST_CAPABILITIES = frozenset({"tasks", "channel"})
ALL_CAPABILITIES = SESSION_CAPABILITIES | HOST_CAPABILITIES
_ID = re.compile(r"[a-z][a-z0-9_-]*\Z")

type ExtensionFactory = Callable[[], SessionExtension | HostExtension]
type Scope = Literal["session", "host"]


@dataclass(frozen=True, slots=True)
class RegisteredExtension:
    manifest: ExtensionManifest
    factory: ExtensionFactory


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


class ExtensionRegistry:
    """Stores factories only; registration and resolution never execute plugin code."""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredExtension] = {}

    def register(self, manifest: ExtensionManifest, factory: ExtensionFactory) -> None:
        if not isinstance(manifest, ExtensionManifest) or not _valid_id(manifest.id):
            raise ManifestError("Extension id must match [a-z][a-z0-9_-]*")
        if manifest.id in self._entries:
            raise ManifestError(f"Duplicate extension id: {manifest.id}")
        if manifest.scope not in ("session", "host"):
            raise ManifestError(f"Invalid scope: {manifest.scope}")
        if not isinstance(manifest.version, str) or not isinstance(manifest.sdk_version_range, str):
            raise ManifestError("Version and SDK version range must be strings")
        try:
            Version(manifest.version)
            compatible = Version(SDK_VERSION) in SpecifierSet(manifest.sdk_version_range)
        except (InvalidVersion, InvalidSpecifier) as exc:
            raise ManifestError(f"Invalid version declaration for {manifest.id}: {exc}") from exc
        if not compatible:
            raise ManifestError(f"{manifest.id} does not support SDK {SDK_VERSION}")
        if not isinstance(manifest.capabilities, tuple) or not all(
            isinstance(capability, str) for capability in manifest.capabilities
        ):
            raise ManifestError("Capabilities must be a tuple of names")
        if len(set(manifest.capabilities)) != len(manifest.capabilities):
            raise ManifestError(f"Duplicate capability in {manifest.id}")
        permitted = SESSION_CAPABILITIES if manifest.scope == "session" else HOST_CAPABILITIES
        invalid = set(manifest.capabilities) - permitted
        if invalid:
            raise CapabilityError(f"Invalid {manifest.scope} capabilities: {sorted(invalid)}")
        if not isinstance(manifest.requires, tuple) or not all(
            _valid_id(dependency) for dependency in manifest.requires
        ):
            raise ManifestError("Requires must be a tuple of extension IDs")
        if len(set(manifest.requires)) != len(manifest.requires):
            raise ManifestError(f"Duplicate dependency in {manifest.id}")
        if not callable(factory):
            raise ManifestError("Extension factory must be callable")
        self._entries[manifest.id] = RegisteredExtension(deepcopy(manifest), factory)

    def fork(self, factories: Mapping[str, ExtensionFactory]) -> ExtensionRegistry:
        """Bind trusted per-session resources without modifying the host registry."""
        if set(factories) - self._entries.keys():
            raise ManifestError("Cannot replace an unregistered factory")
        result = ExtensionRegistry()
        for name, entry in self._entries.items():
            result.register(entry.manifest, factories.get(name, entry.factory))
        return result

    def resolve(
        self,
        enabled: Sequence[str],
        *,
        scope: Scope,
        grants: Mapping[str, frozenset[str]] | None = None,
    ) -> tuple[RegisteredExtension, ...]:
        """Stable topological order; every dependency must be explicitly enabled.

        Among currently eligible extensions, the explicit enablement order wins.
        All identities, dependencies and grants are checked before any factory runs.
        Omitted grants grant nothing, not everything requested by a manifest.
        """
        if scope not in ("session", "host"):
            raise ManifestError(f"Invalid scope: {scope}")
        if (
            not isinstance(enabled, Sequence)
            or isinstance(enabled, (str, bytes))
            or not all(_valid_id(name) for name in enabled)
        ):
            raise DependencyError("Enabled extensions must be a sequence of IDs")
        names = tuple(enabled)
        if len(names) != len(set(names)):
            raise DependencyError("Duplicate enabled extension")
        chosen = set(names)
        permitted = SESSION_CAPABILITIES if scope == "session" else HOST_CAPABILITIES
        for name in names:
            entry = self._entries.get(name)
            if entry is None:
                raise DependencyError(f"Unknown enabled extension: {name}")
            manifest = entry.manifest
            if manifest.scope != scope:
                raise DependencyError(f"{name} is {manifest.scope}-scoped, not {scope}-scoped")
            for dependency in manifest.requires:
                other = self._entries.get(dependency)
                if other is None:
                    raise DependencyError(f"{name} requires missing extension {dependency}")
                if other.manifest.scope != scope:
                    raise DependencyError(f"{name} requires wrong-scope extension {dependency}")
                if dependency not in chosen:
                    raise DependencyError(
                        f"{name} requires explicitly enabled extension {dependency}"
                    )
            allowed = frozenset() if grants is None else grants.get(name, frozenset())
            if not isinstance(allowed, frozenset) or not all(
                isinstance(cap, str) for cap in allowed
            ):
                raise CapabilityError(f"Grants for {name} must be a frozenset of capability names")
            if allowed - permitted:
                raise CapabilityError(
                    f"Invalid {scope} grants for {name}: {sorted(allowed - permitted)}"
                )
            missing = set(manifest.capabilities) - allowed
            if missing:
                raise CapabilityError(f"Capabilities not granted to {name}: {sorted(missing)}")
        ordered: list[RegisteredExtension] = []
        remaining = list(names)
        resolved: set[str] = set()
        while remaining:
            ready = next(
                (
                    name
                    for name in remaining
                    if set(self._entries[name].manifest.requires) <= resolved
                ),
                None,
            )
            if ready is None:
                raise DependencyError(f"Extension dependency cycle: {', '.join(remaining)}")
            ordered.append(self._entries[ready])
            resolved.add(ready)
            remaining.remove(ready)
        return tuple(ordered)
