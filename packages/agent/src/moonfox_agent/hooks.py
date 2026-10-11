"""Hook contracts reserved for stage B; the minimal loop does not dispatch them yet.

The transformation/conversion boundary already runs before every model request.
These DTOs keep request, tool, and turn policy out of the minimal execution loop.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)

from .types import AgentContext, AgentMessage, AgentToolResult


@dataclass(slots=True)
class BeforeToolCallResult:
    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(slots=True)
class AfterToolCallResult:
    content: list[TextContent | ImageContent] | None = None
    details: Any = None
    structured_content: Any = None
    is_error: bool | None = None
    usage: Usage | None = None
    terminate: bool | None = None


@dataclass(slots=True)
class BeforeToolCallContext:
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: Any
    context: AgentContext


@dataclass(slots=True)
class AfterToolCallContext:
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: Any
    result: AgentToolResult
    is_error: bool
    context: AgentContext


@dataclass(slots=True)
class TurnContext:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


@dataclass(slots=True)
class PrepareRequestContext:
    context: AgentContext
    model: Model
    thinking_level: str | None = None


@dataclass(slots=True)
class AgentRequestUpdate:
    """Updates persist for subsequent requests in the same run."""

    context: AgentContext | None = None
    model: Model | None = None
    thinking_level: str | None = None


@dataclass(slots=True)
class AgentLoopTurnUpdate(AgentRequestUpdate):
    messages: list[AgentMessage] | None = None


type TurnDecision = Literal["continue", "end"]
type BeforeToolCall = Callable[
    [BeforeToolCallContext, AbortSignal | None], Awaitable[BeforeToolCallResult | None]
]
type AfterToolCall = Callable[
    [AfterToolCallContext, AbortSignal | None], Awaitable[AfterToolCallResult | None]
]
type FinishTurn = Callable[[TurnContext, AbortSignal | None], Awaitable[TurnDecision | None]]
type PrepareRequest = Callable[
    [PrepareRequestContext, AbortSignal | None], Awaitable[AgentRequestUpdate | None]
]
type PrepareNextTurn = Callable[[TurnContext], Awaitable[AgentLoopTurnUpdate | None]]
