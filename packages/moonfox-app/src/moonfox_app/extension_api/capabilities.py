"""Capability ports injected by the composition root; no concrete service dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from moonfox_agent.api import AgentTool
from moonfox_ai.api import UserMessage

from .types import (
    BoundaryHandler,
    BoundaryPreview,
    BoundaryResult,
    ChannelControl,
    ChannelInput,
    CommandDefinition,
    ContextBlock,
    ContextProvider,
    CustomEntryDraft,
    Delivery,
    EventHandler,
    InputReceipt,
    PermissionDecision,
    PromptProvider,
    PromptSection,
    ReplyEnvelope,
    SessionBinding,
    SessionEvent,
    SessionView,
    TaskInfo,
    ToolPermissionRequest,
)


class RegistrationHandle(Protocol):
    def dispose(self) -> None: ...


class TaskHandle(Protocol):
    @property
    def id(self) -> str: ...

    def cancel(self) -> None: ...

    async def wait(self) -> None: ...


class TaskService(Protocol):
    """Owner-scoped tasks; submit is disabled during setup and after revocation."""

    def submit(
        self, name: str, work: Callable[[], Awaitable[None]], *, timeout: float | None = None
    ) -> TaskHandle: ...

    def list(self) -> tuple[TaskInfo, ...]: ...

    async def cancel_all(self) -> None: ...


class SessionActions(Protocol):
    """Already bound to exactly one session; no caller-supplied session/domain override."""

    def view(self) -> SessionView: ...

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None: ...

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionExtensionContext:
    binding: SessionBinding
    actions: SessionActions
    tasks: TaskService


class SessionExtensionAPI(Protocol):
    def on(self, event: str, handler: EventHandler) -> RegistrationHandle: ...

    def register_tool(self, definition: AgentTool) -> RegistrationHandle: ...

    def register_prompt_section(
        self, provider: PromptProvider, *, required: bool = True
    ) -> RegistrationHandle: ...

    def register_context_provider(self, provider: ContextProvider) -> RegistrationHandle: ...

    def register_command(self, definition: CommandDefinition) -> RegistrationHandle: ...

    def register_boundary(self, handler: BoundaryHandler) -> RegistrationHandle: ...

    def register_tool_check(
        self, checker: Callable[[ToolPermissionRequest], Awaitable[PermissionDecision]]
    ) -> RegistrationHandle: ...

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None: ...

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None: ...

    @property
    def tasks(self) -> TaskService: ...


class ChannelGateway(Protocol):
    async def submit(self, message: ChannelInput) -> InputReceipt: ...

    async def control(self, command: ChannelControl) -> None: ...

    def subscribe_replies(
        self, listener: Callable[[ReplyEnvelope], Awaitable[None]]
    ) -> RegistrationHandle: ...


class HostExtensionAPI(Protocol):
    @property
    def channel(self) -> ChannelGateway: ...

    @property
    def tasks(self) -> TaskService: ...


class SessionExtension(Protocol):
    async def setup(self, api: SessionExtensionAPI) -> None: ...

    async def attach(self, ctx: SessionExtensionContext) -> None: ...

    async def detach(self) -> None: ...

    async def dispose(self) -> None: ...


class HostExtension(Protocol):
    async def setup(self, api: HostExtensionAPI) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def dispose(self) -> None: ...


class SessionExtensionPort(Protocol):
    """Runtime-facing aggregate; implemented by the extension infrastructure."""

    def tools(self) -> tuple[AgentTool, ...]: ...

    async def prompt_sections(self, view: SessionView) -> tuple[PromptSection, ...]: ...

    async def context_blocks(self, view: SessionView) -> tuple[ContextBlock, ...]: ...

    async def notify(self, event: SessionEvent) -> None: ...

    async def check_tool(self, request: ToolPermissionRequest) -> PermissionDecision: ...

    async def boundary(
        self,
        preview: BoundaryPreview,
        validate: Callable[[tuple[CustomEntryDraft, ...]], BoundaryPreview],
    ) -> BoundaryResult: ...
