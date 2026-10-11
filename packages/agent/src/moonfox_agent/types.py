"""Generic run, tool, message, and event contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageEvent,
    ImageContent,
    Message,
    Model,
    SystemMessage,
    TextContent,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
)


class CustomMessage(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def timestamp(self) -> int: ...


type AgentMessage = Message | CustomMessage
type ConvertToLlm = Callable[[list[AgentMessage]], list[Message] | Awaitable[list[Message]]]
type TransformContext = Callable[
    [list[AgentMessage], AbortSignal | None], Awaitable[list[AgentMessage]]
]
type RunReason = Literal["completed", "aborted", "error", "limit_reached"]


def default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """Never silently lose an unknown message; custom roles require an explicit codec."""
    converted: list[Message] = []
    for message in messages:
        if not isinstance(
            message, (SystemMessage, UserMessage, AssistantMessage, ToolResultMessage)
        ):
            raise TypeError(f"Custom message role {message.role!r} requires convert_to_llm")
        converted.append(deepcopy(message))
    return converted


@dataclass(frozen=True, slots=True)
class RunOptions:
    max_turns_per_run: int

    def __post_init__(self) -> None:
        value = self.max_turns_per_run
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("max_turns_per_run must be an integer >= 1")


@dataclass(frozen=True, slots=True)
class RunOutcome:
    reason: RunReason
    turns_used: int


@dataclass(slots=True)
class AgentToolResult:
    content: list[TextContent | ImageContent]
    details: Any = None
    structured_content: Any = None
    usage: Usage | None = None
    is_error: bool = False
    terminate: bool = False


type ToolUpdate = Callable[[AgentToolResult], Awaitable[None]]
type ToolExecute = Callable[
    [str, Any, AbortSignal | None, ToolUpdate | None], Awaitable[AgentToolResult]
]


@dataclass(frozen=True, slots=True)
class AgentTool:
    """Injected implementation with Pydantic input validation.

    execute(call_id, validated_args, signal, on_update) returns a typed result.
    Updates are awaited; no detached update tasks survive a tool's completion.
    Use ToolExecutionError/OSError or is_error=True for operational failures.
    Other exceptions are programmer errors and remain visible to the caller.
    """

    name: str
    description: str
    label: str
    parameters: type[BaseModel]
    execute: ToolExecute

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(self.name, self.description, self.parameters.model_json_schema())


@dataclass(slots=True)
class AgentContext:
    messages: list[AgentMessage]
    tools: list[AgentTool] = field(default_factory=list)


@dataclass(slots=True)
class AgentLoopConfig:
    model: Model
    options: RunOptions
    convert_to_llm: ConvertToLlm = default_convert_to_llm
    transform_context: TransformContext | None = None


@dataclass(slots=True)
class AgentStart:
    type: Literal["agent_start"] = field(default="agent_start", init=False)


@dataclass(slots=True)
class AgentEnd:
    messages: list[AgentMessage]
    outcome: RunOutcome
    type: Literal["agent_end"] = field(default="agent_end", init=False)


@dataclass(slots=True)
class TurnStart:
    turn: int
    type: Literal["turn_start"] = field(default="turn_start", init=False)


@dataclass(slots=True)
class TurnEnd:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    turn: int
    type: Literal["turn_end"] = field(default="turn_end", init=False)


@dataclass(slots=True)
class MessageStart:
    message: AgentMessage
    message_id: str
    type: Literal["message_start"] = field(default="message_start", init=False)


@dataclass(slots=True)
class MessageUpdate:
    message: AssistantMessage
    assistant_message_event: AssistantMessageEvent
    message_id: str
    type: Literal["message_update"] = field(default="message_update", init=False)


@dataclass(slots=True)
class MessageEnd:
    message: AgentMessage
    message_id: str
    type: Literal["message_end"] = field(default="message_end", init=False)


@dataclass(slots=True)
class ToolExecutionStart:
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    type: Literal["tool_execution_start"] = field(default="tool_execution_start", init=False)


@dataclass(slots=True)
class ToolExecutionUpdate:
    tool_call_id: str
    tool_name: str
    partial_result: AgentToolResult
    type: Literal["tool_execution_update"] = field(default="tool_execution_update", init=False)


@dataclass(slots=True)
class ToolExecutionEnd:
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool
    duration_ms: int | None = None
    type: Literal["tool_execution_end"] = field(default="tool_execution_end", init=False)


type AgentEvent = (
    AgentStart
    | AgentEnd
    | TurnStart
    | TurnEnd
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolExecutionStart
    | ToolExecutionUpdate
    | ToolExecutionEnd
)
type EventSink = Callable[[AgentEvent], Awaitable[None]]
