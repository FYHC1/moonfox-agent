"""Session-scoped capability adapters and the runtime-facing extension aggregate."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import cast

from pydantic import BaseModel

from moonfox_agent.api import AgentTool
from moonfox_ai.api import UserMessage
from moonfox_app.extension_api import (
    BoundaryHandler,
    BoundaryPreview,
    BoundaryResult,
    CommandDefinition,
    ContextBlock,
    ContextProvider,
    CustomEntryDraft,
    Delivery,
    EventHandler,
    HostExtension,
    PermissionChecker,
    PermissionDecision,
    PromptProvider,
    PromptSection,
    RegistrationHandle,
    SessionActions,
    SessionEvent,
    SessionExtension,
    SessionExtensionContext,
    SessionView,
    TaskService,
    ToolPermissionRequest,
)

from . import validation
from .dispatch import Dispatcher, Handler, cancellation_pending, invoke
from .errors import CommandAuthorizationError, CommandNotFoundError, RegistrationError
from .lifecycle import Instance, LifecycleManager, Owner
from .registry import ExtensionRegistry


@dataclass(frozen=True, slots=True, eq=False)
class Contribution[T]:
    owner: Owner
    value: T
    name: str | None = None


class Contributions[T]:
    def __init__(self) -> None:
        self.items: list[Contribution[T]] = []

    def add(self, owner: Owner, value: T, *, name: str | None = None) -> RegistrationHandle:
        if name is not None and any(item.name == name for item in self.items):
            raise RegistrationError(f"Duplicate contribution: {name}")
        item = Contribution(owner, value, name)
        self.items.append(item)
        return owner.track(lambda: self.items.remove(item))


class SessionRegistrations:
    def __init__(self) -> None:
        self.events: Contributions[tuple[str, EventHandler]] = Contributions()
        self.tools: Contributions[AgentTool] = Contributions()
        self.prompts: Contributions[tuple[PromptProvider, bool]] = Contributions()
        self.context: Contributions[ContextProvider] = Contributions()
        self.commands: Contributions[CommandDefinition] = Contributions()
        self.boundaries: Contributions[BoundaryHandler] = Contributions()
        self.checks: Contributions[PermissionChecker] = Contributions()


def _callback(callback: object) -> None:
    if not callable(callback):
        raise RegistrationError("A callable handler/provider is required")


class ScopedSessionActions:
    def __init__(self, owner: Owner, actions: SessionActions) -> None:
        self._owner = owner
        self._actions: SessionActions | None = actions
        owner.track(self._release)

    def _release(self) -> None:
        self._actions = None

    def _port(self, capability: str | None = None) -> SessionActions:
        self._owner.require(capability, active=True)
        assert self._actions is not None
        return self._actions

    def view(self) -> SessionView:
        return deepcopy(self._port().view())

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None:
        port = self._port("entries")
        port.stage_entries(validation.drafts(self._owner.manifest.id, drafts))

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        port = self._port("enqueue")
        port.enqueue(validation.user_message(message, delivery), delivery)


class SessionAPI:
    def __init__(
        self, owner: Owner, registrations: SessionRegistrations, actions: ScopedSessionActions
    ) -> None:
        self._owner = owner
        self._registrations = registrations
        self._actions = actions
        owner.track(self._release)

    def _release(self) -> None:
        # An old API retained by a plugin must not retain other/current plugin instances.
        self._registrations = SessionRegistrations()

    def on(self, event: str, handler: EventHandler) -> RegistrationHandle:
        self._owner.require("events")
        if not isinstance(event, str) or not event:
            raise RegistrationError("Event name must be nonempty")
        _callback(handler)
        return self._registrations.events.add(self._owner, (event, handler))

    def register_tool(self, definition: AgentTool) -> RegistrationHandle:
        self._owner.require("tools")
        if not isinstance(definition, AgentTool):
            raise RegistrationError("Tools must be AgentTool definitions")
        validation.namespace(self._owner.manifest.id, definition.name)
        if (
            not isinstance(definition.description, str)
            or not isinstance(definition.label, str)
            or not isinstance(definition.parameters, type)
            or not issubclass(definition.parameters, BaseModel)
        ):
            raise RegistrationError("Tool parameters must be a Pydantic model with a JSON schema")
        try:
            json.dumps(definition.parameters.model_json_schema(), allow_nan=False)
        except Exception as exc:
            raise RegistrationError(f"Invalid tool parameter schema: {definition.name}") from exc
        # Do NOT copy tools: bound implementations can own uncopyable injected services.
        return self._registrations.tools.add(self._owner, definition, name=definition.name)

    def register_prompt_section(
        self, provider: PromptProvider, *, required: bool = True
    ) -> RegistrationHandle:
        self._owner.require("prompt")
        _callback(provider)
        if type(required) is not bool:
            raise RegistrationError("Prompt required flag must be boolean")
        return self._registrations.prompts.add(self._owner, (provider, required))

    def register_context_provider(self, provider: ContextProvider) -> RegistrationHandle:
        self._owner.require("context")
        _callback(provider)
        return self._registrations.context.add(self._owner, provider)

    def register_command(self, definition: CommandDefinition) -> RegistrationHandle:
        self._owner.require("commands")
        if not isinstance(definition, CommandDefinition) or not isinstance(
            definition.description, str
        ):
            raise RegistrationError("Commands must be CommandDefinition")
        validation.namespace(self._owner.manifest.id, definition.name)
        _callback(definition.handler)
        # Copy metadata, not the bound callback or its service graph.
        command = CommandDefinition(definition.name, definition.description, definition.handler)
        return self._registrations.commands.add(self._owner, command, name=command.name)

    def register_boundary(self, handler: BoundaryHandler) -> RegistrationHandle:
        self._owner.require("boundary")
        _callback(handler)
        return self._registrations.boundaries.add(self._owner, handler)

    def register_tool_check(self, checker: PermissionChecker) -> RegistrationHandle:
        self._owner.require("tool_check")
        _callback(checker)
        return self._registrations.checks.add(self._owner, checker)

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None:
        self._actions.stage_entries(drafts)

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        self._actions.enqueue(message, delivery)

    @property
    def tasks(self) -> TaskService:
        self._owner.require("tasks")
        return self._owner.tasks


class SessionExtensionManager(LifecycleManager):
    """Implements SessionExtensionPort; lifecycle mutation is idle-guarded and serialized."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        actions: SessionActions,
        enabled: Sequence[str] = (),
        grants: Mapping[str, frozenset[str]] | None = None,
        timeout: float = 5.0,
        ensure_idle: Callable[[], None] | None = None,
        allowed_tools: frozenset[str] | None = None,
    ) -> None:
        super().__init__(
            registry,
            scope="session",
            enabled=enabled,
            grants=grants,
            timeout=timeout,
            ensure_idle=ensure_idle,
        )
        self._actions = actions
        self._allowed_tools = allowed_tools
        self._registrations = SessionRegistrations()
        self._dispatcher = Dispatcher(timeout=timeout, diagnose=self._diagnostics.append)

    def _bind(self, owner: Owner, plugin: SessionExtension | HostExtension) -> Instance:
        session = cast(SessionExtension, plugin)
        actions = ScopedSessionActions(owner, self._actions)
        api = SessionAPI(owner, self._registrations, actions)

        async def attach() -> None:
            await session.attach(
                SessionExtensionContext(actions.view().binding, actions, owner.tasks)
            )

        return Instance(
            owner,
            lambda: session.setup(api),
            attach,
            lambda: session.detach(),
            lambda: session.dispose(),
        )

    def _snapshot[T](self, contributions: Contributions[T]) -> tuple[Contribution[T], ...]:
        if not self._started:
            return ()
        return tuple(item for item in contributions.items if item.owner.phase == "active")

    def tools(self) -> tuple[AgentTool, ...]:
        return tuple(
            item.value
            for item in self._snapshot(self._registrations.tools)
            if self._allowed_tools is None or item.value.name in self._allowed_tools
        )

    async def notify(self, event: SessionEvent) -> None:
        handlers = tuple(
            Handler(item.owner.manifest.id, item.value[1])
            for item in self._snapshot(self._registrations.events)
            if item.value[0] == event.type
        )
        await self._dispatcher.notify(handlers, event, operation=f"event:{event.type}")

    async def prompt_sections(self, view: SessionView) -> tuple[PromptSection, ...]:
        contributions = self._snapshot(self._registrations.prompts)
        snapshot = deepcopy(view)
        sections: list[PromptSection] = []
        names: set[str] = set()
        for item in contributions:
            provider, required = item.value
            try:
                result = await invoke(partial(provider, deepcopy(snapshot)), self._timeout)
                candidate = validation.prompt(item.owner.manifest.id, result)
                if any(section.name in names for section in candidate):
                    raise RegistrationError("Conflicting prompt section names across providers")
                sections.extend(candidate)
                names.update(section.name for section in candidate)
            except (Exception, asyncio.CancelledError) as exc:
                item.owner.failure("prompt", exc)
                if required or (isinstance(exc, asyncio.CancelledError) and cancellation_pending()):
                    raise
        return tuple(sections)

    async def context_blocks(self, view: SessionView) -> tuple[ContextBlock, ...]:
        contributions = self._snapshot(self._registrations.context)
        snapshot = deepcopy(view)
        blocks: list[ContextBlock] = []
        for item in contributions:
            try:
                result = await invoke(partial(item.value, deepcopy(snapshot)), self._timeout)
                blocks.extend(validation.context(result))
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                item.owner.failure("context", exc)
        return tuple(blocks)

    async def check_tool(self, request: ToolPermissionRequest) -> PermissionDecision:
        # The mandatory host checker runs FIRST in the runtime, outside plugin registrations.
        handlers = tuple(
            Handler[ToolPermissionRequest, PermissionDecision](item.owner.manifest.id, item.value)
            for item in self._snapshot(self._registrations.checks)
        )
        return await self._dispatcher.gate(handlers, request, operation="tool_check")

    async def boundary(
        self,
        preview: BoundaryPreview,
        validate: Callable[[tuple[CustomEntryDraft, ...]], BoundaryPreview],
    ) -> BoundaryResult:
        handlers = self._snapshot(self._registrations.boundaries)
        current = deepcopy(preview)
        cumulative = deepcopy(preview.drafts)
        continue_ = False
        for item in handlers:
            try:
                result = await invoke(partial(item.value, deepcopy(current)), self._timeout)
                if result is not None:
                    if (
                        not isinstance(result, BoundaryResult)
                        or not isinstance(result.drafts, tuple)
                        or type(result.continue_) is not bool
                    ):
                        raise RegistrationError(
                            "Boundary handler must return BoundaryResult or None"
                        )
                    cumulative += validation.drafts(item.owner.manifest.id, result.drafts)
                    continue_ = continue_ or result.continue_
                validated = validate(deepcopy(cumulative))
                if not isinstance(validated, BoundaryPreview) or validated.drafts != cumulative:
                    raise RegistrationError(
                        "Boundary validator must preserve the cumulative drafts"
                    )
                current = deepcopy(validated)
            except (Exception, asyncio.CancelledError) as exc:
                item.owner.failure("boundary", exc)
                raise  # Caller owns the transaction: no partial result or continuation on failure.
        return BoundaryResult(deepcopy(cumulative), continue_)

    async def execute_command(
        self, name: str, argument: str, *, authorize: Callable[[str], Awaitable[bool]]
    ) -> str:
        item = next(
            (item for item in self._snapshot(self._registrations.commands) if item.name == name),
            None,
        )
        if item is None:
            raise CommandNotFoundError(name)
        if not isinstance(argument, str):
            raise RegistrationError("Command argument must be a string")
        try:
            allowed = await invoke(lambda: authorize(name), self._timeout)
            if allowed is not True:
                raise CommandAuthorizationError(f"Command not authorized: {name}")
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                raise
            item.owner.failure("command.authorize", exc)
            raise CommandAuthorizationError(f"Command not authorized: {name}") from exc
        item.owner.require("commands", active=True)
        if not self._started or item not in self._registrations.commands.items:
            raise CommandNotFoundError(name)
        try:
            result = await invoke(lambda: item.value.handler(argument), self._timeout)
            if not isinstance(result, str):
                raise RegistrationError("Command handler must return a string")
            return result
        except (Exception, asyncio.CancelledError) as exc:
            item.owner.failure("command", exc)
            raise
