"""Versioned, detached data contracts; no runtime, storage, or host imports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from moonfox_agent.api import AgentMessage, RunOutcome
from moonfox_ai.api import Message, Model

SDK_VERSION = "0.1.0"
type Delivery = Literal["steer", "follow_up"]


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    id: str
    version: str
    sdk_version_range: str
    scope: Literal["session", "host"]
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionBinding:
    """Injected immutable identity. Runtime treats scope_id as opaque, not a routing key."""

    session_id: str
    scope_id: str

    def __post_init__(self) -> None:
        if not self.session_id or not self.scope_id:
            raise ValueError("Session and scope IDs must be nonempty")


@dataclass(frozen=True, slots=True)
class SessionView:
    binding: SessionBinding
    revision: int
    leaf_id: str
    messages: tuple[AgentMessage, ...]
    model: Model
    thinking_level: str


@dataclass(frozen=True, slots=True)
class PromptSection:
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """Untrusted reference data, rendered as user content, never system/tool declarations."""

    source: str
    text: str


@dataclass(frozen=True, slots=True)
class CustomEntryDraft:
    custom_type: str
    data: Any = None
    context_required: bool = False


@dataclass(frozen=True, slots=True)
class BoundaryPreview:
    view: SessionView
    drafts: tuple[CustomEntryDraft, ...] = ()
    outcome: RunOutcome | None = None


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    drafts: tuple[CustomEntryDraft, ...] = ()
    continue_: bool = False


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolPermissionRequest:
    binding: SessionBinding
    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]
    parent_call_id: str | None = None
    depth: int = 0


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    name: str
    description: str
    handler: Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ExtensionDiagnostic:
    extension_id: str
    operation: str
    message: str


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Application event envelope, distinct from the core AgentEvent union.

    Payloads and views are detached per listener. This is not the future wire protocol.
    """

    type: str
    binding: SessionBinding
    revision: int
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ChannelInput:
    connection_id: str
    bot_account: str
    target: str
    speaker: str
    external_message_id: str
    received_at: int
    content: str


@dataclass(frozen=True, slots=True)
class ChannelControl:
    connection_id: str
    target: str
    action: Literal["cancel"]


@dataclass(frozen=True, slots=True)
class InputReceipt:
    input_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class ReplyEnvelope:
    run_id: str
    message_id: str
    target: str
    text: str


@dataclass(frozen=True, slots=True)
class TaskInfo:
    id: str
    name: str
    done: bool
    cancelled: bool


type EventHandler = Callable[[SessionEvent], Awaitable[None] | None]
type PromptProvider = Callable[[SessionView], Awaitable[tuple[PromptSection, ...]]]
type ContextProvider = Callable[[SessionView], Awaitable[tuple[ContextBlock, ...]]]
type BoundaryHandler = Callable[[BoundaryPreview], Awaitable[BoundaryResult | None]]
type PermissionChecker = Callable[[ToolPermissionRequest], Awaitable[PermissionDecision]]
type CustomEntryCodec = Callable[[CustomEntryDraft, int], list[Message]]


@dataclass(frozen=True, slots=True)
class ExtensionContributions:
    """A single detached provider snapshot. Failed context providers contribute nothing."""

    sections: tuple[PromptSection, ...] = ()
    context: tuple[ContextBlock, ...] = ()
    drafts: tuple[CustomEntryDraft, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
