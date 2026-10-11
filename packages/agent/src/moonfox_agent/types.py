"""Generic run, tool, message, and event contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    AssistantMessageEvent,
    ImageContent,
    Message,
    Model,
    NestedToolCall,
    StreamOptions,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
)

if TYPE_CHECKING:
    from .hooks import AfterToolCall, BeforeToolCall, FinishTurn, PrepareNextTurn, PrepareRequest


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
type ToolExecutionMode = Literal["sequential", "parallel"]


def default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    """Never silently lose an unknown message; custom roles require an explicit codec."""
    converted: list[Message] = []
    for message in messages:
        if not isinstance(
            message, (SystemMessage, UserMessage, AssistantMessage, ToolResultMessage)
        ):
            raise TypeError(f"Custom message role {message.role!r} requires convert_to_llm")
        snapshot = deepcopy(message)
        if isinstance(snapshot, ToolResultMessage):
            snapshot.nested_calls = None
            snapshot.details = None
            snapshot.structured_content = None
        converted.append(snapshot)
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
class ToolCallLimits:
    """Per-turn limits shared by every top-level and nested invocation."""

    max_depth: int = 8
    max_calls: int = 128

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("max_depth", self.max_depth, 0),
            ("max_calls", self.max_calls, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(slots=True)
class AgentToolCallOutcome:
    tool_call: ToolCall
    result: AgentToolResult
    duration_ms: int
    nested_calls: list[NestedToolCall] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.result.is_error


class ToolExecutionContext(Protocol):
    @property
    def signal(self) -> AbortSignal | None: ...

    @property
    def tool_call_id(self) -> str: ...

    @property
    def parent_call_id(self) -> str | None: ...

    @property
    def depth(self) -> int: ...

    async def update(self, result: AgentToolResult) -> None: ...

    async def execute_tool(self, name: str, args: dict[str, Any]) -> AgentToolCallOutcome: ...


type ContextToolExecute = Callable[[str, Any, ToolExecutionContext], Awaitable[AgentToolResult]]


@dataclass(frozen=True, slots=True)
class AgentTool:
    """Injected implementation with Pydantic input validation.

    Supply either the compatible four-argument execute callback or
    execute_with_context(call_id, validated_args, context), never both.
    Context-based tools can invoke checked children and send awaited updates.
    Operational failures use ToolExecutionError/OSError or is_error=True;
    programmer errors propagate after the tool batch settles and is paired.
    """

    name: str
    description: str
    label: str
    parameters: type[BaseModel]
    execute: ToolExecute | None = None
    execute_with_context: ContextToolExecute | None = None
    prepare_arguments: Callable[[Any], Any] | None = None
    execution_mode: ToolExecutionMode | None = None
    replay: Literal["never", "safe"] = "never"

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Tool name must be a nonempty string")
        if (self.execute is None) == (self.execute_with_context is None):
            raise ValueError("Supply exactly one tool execution callback")
        if self.execution_mode not in (None, "parallel", "sequential"):
            raise ValueError("Invalid tool execution mode")
        if self.replay not in ("never", "safe"):
            raise ValueError("Invalid tool replay policy")

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(self.name, self.description, self.parameters.model_json_schema())


@dataclass(slots=True)
class AgentContext:
    messages: list[AgentMessage]
    tools: list[AgentTool] = field(default_factory=list)


class MessageQueue(Protocol):
    """Nonblocking queue port; peek is required to avoid consuming at the run limit."""

    def has_items(self) -> bool: ...

    def peek(self) -> list[AgentMessage]: ...

    def drain(self) -> list[AgentMessage]: ...


@dataclass(slots=True)
class AgentLoopConfig:
    model: Model
    options: RunOptions
    convert_to_llm: ConvertToLlm = default_convert_to_llm
    transform_context: TransformContext | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    finish_turn: FinishTurn | None = None
    prepare_request: PrepareRequest | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    steering_queue: MessageQueue | None = None
    follow_up_queue: MessageQueue | None = None
    tool_execution: ToolExecutionMode = "parallel"
    tool_limits: ToolCallLimits = field(default_factory=ToolCallLimits)
    thinking_level: str = "off"
    stream_options: StreamOptions = field(default_factory=StreamOptions)
    get_api_key: Callable[[str], Awaitable[str | None]] | None = None

    def __post_init__(self) -> None:
        if self.tool_execution not in ("parallel", "sequential"):
            raise ValueError("Invalid tool execution mode")
        if not isinstance(self.options, RunOptions):
            raise TypeError("AgentLoopConfig requires RunOptions")
        if not isinstance(self.tool_limits, ToolCallLimits):
            raise TypeError("AgentLoopConfig requires ToolCallLimits")


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
