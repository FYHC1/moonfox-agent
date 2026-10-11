"""Injected request, tool and turn hooks. Arguments are detached snapshots.

prepare_request runs on EVERY request after pending inputs have been appended.
prepare_next_turn runs only when another request is scheduled, never on the first.
finish_turn: None uses normal scheduling; end skips queue polling; continue is
satisfied by one natural request, or one context-only request within the budget.
Error/abort outcomes ignore continuation decisions. Permission failures fail closed.
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
    """Replace supplied fields, without merging; content invalidates old structured data.

    An existing failure can never be converted into success by a result transformer.
    None means omitted for optional fields; use content=[] for an empty replacement.
    """

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
    parent_call_id: str | None = None
    depth: int = 0


@dataclass(slots=True)
class AfterToolCallContext:
    assistant_message: AssistantMessage
    tool_call: ToolCall
    args: Any
    result: AgentToolResult
    is_error: bool
    context: AgentContext
    parent_call_id: str | None = None
    depth: int = 0


@dataclass(slots=True)
class TurnContext:
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[AgentMessage]
    turn: int = 0


@dataclass(slots=True)
class PrepareRequestContext:
    context: AgentContext
    model: Model
    thinking_level: str | None = None
    canonical_messages: list[AgentMessage] | None = None
    """Unaugmented transcript for rebuilding a request projection without duplication."""


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
