"""Shared ownership, revocation, and serialized lifecycle (no application imports)."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from moonfox_app.extension_api import (
    ExtensionDiagnostic,
    ExtensionManifest,
    HostExtension,
    SessionExtension,
)

from .dispatch import invoke, validate_timeout
from .errors import (
    CapabilityError,
    DependencyError,
    ExtensionRevokedError,
    LifecycleError,
    describe,
)
from .registry import ExtensionRegistry, RegisteredExtension, Scope
from .tasks import OwnedTaskService


class Handle:
    """Idempotent disposal also releases closures retained by a plugin's old handle."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release: Callable[[], None] | None = release

    def dispose(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            release()


class Owner:
    def __init__(
        self, manifest: ExtensionManifest, diagnose: Callable[[ExtensionDiagnostic], None]
    ) -> None:
        self.manifest = manifest
        self.phase: Literal["setup", "active", "revoked"] = "setup"
        self._diagnose = diagnose
        self._handles: list[Handle] = []
        self.tasks = OwnedTaskService(
            manifest.id, lambda: self.require("tasks", active=True), diagnose
        )

    def require(self, capability: str | None = None, *, active: bool = False) -> None:
        if self.phase == "revoked":
            raise ExtensionRevokedError(f"{self.manifest.id} has been revoked")
        if capability is not None and capability not in self.manifest.capabilities:
            raise CapabilityError(f"{self.manifest.id} did not request {capability}")
        if active and self.phase != "active":
            raise LifecycleError(
                f"{self.manifest.id}: actions and tasks are unavailable during setup"
            )

    def track(self, release: Callable[[], None]) -> Handle:
        self.require()

        def dispose() -> None:
            self._handles.remove(handle)
            release()

        handle = Handle(dispose)
        self._handles.append(handle)
        return handle

    def revoke(self) -> None:
        self.phase = "revoked"
        self.tasks.close()
        for handle in reversed(tuple(self._handles)):
            try:
                handle.dispose()
            except BaseException as exc:
                self.failure("revoke", exc)

    def failure(self, operation: str, exc: BaseException) -> None:
        self._diagnose(ExtensionDiagnostic(self.manifest.id, operation, describe(exc)))


@dataclass(slots=True)
class Instance:
    owner: Owner
    setup: Callable[[], Awaitable[None]]
    activate: Callable[[], Awaitable[None]]
    deactivate: Callable[[], Awaitable[None]]
    dispose: Callable[[], Awaitable[None]]
    attempted_activation: bool = False


class LifecycleManager(ABC):
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        scope: Scope,
        enabled: Sequence[str],
        grants: Mapping[str, frozenset[str]] | None,
        timeout: float,
        ensure_idle: Callable[[], None] | None = None,
    ) -> None:
        validate_timeout(timeout)
        if not isinstance(enabled, Sequence) or isinstance(enabled, (str, bytes)):
            raise DependencyError("Enabled extensions must be a sequence of IDs")
        self._registry = registry
        self._scope = scope
        self._enabled = tuple(enabled)
        self._grants = None if grants is None else dict(grants)
        self._timeout = timeout
        self._ensure_idle = ensure_idle
        self._diagnostics: deque[ExtensionDiagnostic] = deque(maxlen=1024)
        self._instances: list[Instance] = []
        self._started = False
        self._lock = asyncio.Lock()
        self._mutating: asyncio.Task[object] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def diagnostics(self) -> tuple[ExtensionDiagnostic, ...]:
        return tuple(self._diagnostics)

    @property
    def active_ids(self) -> tuple[str, ...]:
        return (
            tuple(
                instance.owner.manifest.id
                for instance in self._instances
                if instance.owner.phase == "active"
            )
            if self._started
            else ()
        )

    def _mutation_guard(self) -> None:
        current = asyncio.current_task()
        if current is self._mutating or current is self._cleanup_task:
            raise LifecycleError("Reentrant extension lifecycle mutation")
        if any(instance.owner.tasks.owns_current_task() for instance in self._instances):
            raise LifecycleError("An owned task cannot mutate its manager's lifecycle")

    def _idle(self) -> None:
        if self._ensure_idle is not None:
            self._ensure_idle()

    def _preflight(self) -> tuple[RegisteredExtension, ...]:
        return self._registry.resolve(self._enabled, scope=self._scope, grants=self._grants)

    @abstractmethod
    def _bind(self, owner: Owner, plugin: SessionExtension | HostExtension) -> Instance: ...

    async def start(self) -> None:
        self._mutation_guard()
        async with self._lock:
            if self._started:
                return
            self._idle()
            entries = self._preflight()
            self._mutating = asyncio.current_task()
            try:
                for entry in entries:
                    owner = Owner(entry.manifest, self._diagnostics.append)
                    try:
                        instance = self._bind(owner, entry.factory())
                        self._instances.append(instance)
                        await invoke(instance.setup, self._timeout)
                    except BaseException as exc:
                        owner.failure("setup", exc)
                        owner.revoke()
                        raise
                for instance in self._instances:
                    instance.owner.phase = "active"
                    instance.attempted_activation = True
                    try:
                        await invoke(instance.activate, self._timeout)
                    except BaseException as exc:
                        instance.owner.failure(
                            "attach" if self._scope == "session" else "start", exc
                        )
                        raise
                self._started = True
            except BaseException:
                # A later cleanup exception/cancellation must not replace the original.
                await self._finish_cleanup(tuple(self._instances))
                self._instances.clear()
                raise
            finally:
                self._mutating = None

    async def stop(self) -> None:
        self._mutation_guard()
        async with self._lock:
            if not self._started:
                return
            self._idle()
            self._mutating = asyncio.current_task()
            self._started = False
            try:
                cancelled = await self._finish_cleanup(tuple(self._instances))
                self._instances.clear()
                if cancelled is not None:
                    raise cancelled
            finally:
                self._mutating = None

    async def disable(self, extension_id: str) -> None:
        self._mutation_guard()
        async with self._lock:
            if extension_id not in self._enabled:
                return
            self._idle()
            dependents = [
                instance.owner.manifest.id
                for instance in self._instances
                if instance.owner.phase == "active"
                and extension_id in instance.owner.manifest.requires
            ]
            if dependents:
                raise DependencyError(f"{extension_id} is required by {', '.join(dependents)}")
            self._mutating = asyncio.current_task()
            try:
                targets = tuple(
                    instance
                    for instance in self._instances
                    if instance.owner.manifest.id == extension_id
                )
                cancelled = await self._finish_cleanup(targets) if targets else None
                self._instances = [item for item in self._instances if item not in targets]
                self._enabled = tuple(name for name in self._enabled if name != extension_id)
                if cancelled is not None:
                    raise cancelled
            finally:
                self._mutating = None

    async def _finish_cleanup(
        self, instances: tuple[Instance, ...]
    ) -> asyncio.CancelledError | None:
        """Drain one tracked cleanup task despite repeated caller cancellation."""
        for instance in reversed(instances):
            instance.owner.revoke()
        task = asyncio.create_task(
            self._cleanup(instances), name=f"extensions:{self._scope}:cleanup"
        )
        self._cleanup_task = task
        cancelled: asyncio.CancelledError | None = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            task.result()
        finally:
            self._cleanup_task = None
        return cancelled

    async def _cleanup(self, instances: tuple[Instance, ...]) -> None:
        # Set before any await, including with an eager event-loop task factory.
        self._cleanup_task = asyncio.current_task()
        for instance in reversed(instances):
            steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
                ("tasks.cancel_all", instance.owner.tasks.cancel_all)
            ]
            if instance.attempted_activation:
                steps.append(
                    ("detach" if self._scope == "session" else "stop", instance.deactivate)
                )
            steps.append(("dispose", instance.dispose))
            for operation, callback in steps:
                try:
                    await invoke(callback, self._timeout)
                except BaseException as exc:
                    instance.owner.failure(operation, exc)
