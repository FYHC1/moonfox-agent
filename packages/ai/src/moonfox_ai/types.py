"""Provider-neutral messages and events. Live partials are not stable snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

type SuccessReason = Literal["stop", "length", "tool_use", "deferred"]
type FailureReason = Literal["error", "aborted"]
type StopReason = Literal["pending"] | SuccessReason | FailureReason


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class Cost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    reasoning: int | None = None
    cost: Cost = field(default_factory=Cost)

    @property
    def effective_total(self) -> int:
        """Reasoning is a subset of output, not an additional token charge."""
        return self.total_tokens or (self.input + self.output + self.cache_read + self.cache_write)


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    provider: str
    api: str
    name: str | None = None
    base_url: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    reasoning: bool = False
    input: tuple[Literal["text", "image"], ...] = ("text",)


@dataclass(slots=True)
class TextContent:
    text: str
    text_signature: str | None = None
    type: Literal["text"] = field(default="text", init=False)


@dataclass(slots=True)
class ThinkingContent:
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False
    type: Literal["thinking"] = field(default="thinking", init=False)


@dataclass(slots=True)
class ImageContent:
    data: str
    mime_type: str
    type: Literal["image"] = field(default="image", init=False)


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    type: Literal["toolCall"] = field(default="toolCall", init=False)


type ContentBlock = TextContent | ThinkingContent | ImageContent | ToolCall


@dataclass(slots=True)
class ToolDeclaration:
    name: str
    description: str
    parameters: dict[str, Any]


class HasToolDeclaration(Protocol):
    def declaration(self) -> ToolDeclaration: ...


@dataclass(slots=True)
class SystemMessage:
    """Append content, replace/delete named sections, and apply tool deltas in order."""

    content: str | list[TextContent]
    timestamp: int
    sections: dict[str, str | None] | None = None
    tools_added: list[ToolDeclaration] | None = None
    tools_removed: list[str] | None = None
    role: Literal["system"] = field(default="system", init=False)


@dataclass(slots=True)
class UserMessage:
    content: str | list[TextContent | ImageContent]
    timestamp: int
    role: Literal["user"] = field(default="user", init=False)


@dataclass(slots=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    is_error: bool
    timestamp: int
    details: Any = None
    structured_content: Any = None
    usage: Usage | None = None
    nested_calls: list[NestedToolCall] | None = None
    duration_ms: int | None = None
    role: Literal["toolResult"] = field(default="toolResult", init=False)


@dataclass(slots=True)
class NestedToolCall:
    """Audit-only nested invocation; never part of a model request's tool results."""

    tool_call: ToolCall
    result: ToolResultMessage


@dataclass(slots=True)
class AssistantMessage:
    content: list[ContentBlock]
    api: str
    provider: str
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: int
    response_model: str | None = None
    response_id: str | None = None
    provider_thinking_level: str | None = None
    thinking_level: str | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    end_turn: bool | None = None
    duration_ms: int | None = None
    diagnostics: list[dict[str, Any]] | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)


type Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage


@dataclass(slots=True)
class Context:
    messages: list[Message]
    system_prompt: str | None = None
    tools: list[ToolDeclaration] | None = None


_BRAND = object()


class TranscriptContext:
    """Branded normalized context. Construct via normalize_context(), not directly."""

    __slots__ = ("messages",)

    def __init__(self, messages: list[Message], _token: object = None) -> None:
        if _token is not _BRAND:
            raise TypeError("Use normalize_context() to create TranscriptContext")
        self.messages = messages


@dataclass(slots=True)
class Start:
    partial: AssistantMessage
    type: Literal["start"] = field(default="start", init=False)


@dataclass(slots=True)
class TextStart:
    content_index: int
    partial: AssistantMessage
    type: Literal["text_start"] = field(default="text_start", init=False)


@dataclass(slots=True)
class TextDelta:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = field(default="text_delta", init=False)


@dataclass(slots=True)
class TextEnd:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["text_end"] = field(default="text_end", init=False)


@dataclass(slots=True)
class ThinkingStart:
    content_index: int
    partial: AssistantMessage
    type: Literal["thinking_start"] = field(default="thinking_start", init=False)


@dataclass(slots=True)
class ThinkingDelta:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["thinking_delta"] = field(default="thinking_delta", init=False)


@dataclass(slots=True)
class ThinkingEnd:
    content_index: int
    content: str
    partial: AssistantMessage
    type: Literal["thinking_end"] = field(default="thinking_end", init=False)


@dataclass(slots=True)
class ToolCallStart:
    content_index: int
    partial: AssistantMessage
    type: Literal["toolcall_start"] = field(default="toolcall_start", init=False)


@dataclass(slots=True)
class ToolCallDelta:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["toolcall_delta"] = field(default="toolcall_delta", init=False)


@dataclass(slots=True)
class ToolCallEnd:
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = field(default="toolcall_end", init=False)


@dataclass(slots=True)
class Done:
    reason: SuccessReason
    message: AssistantMessage
    type: Literal["done"] = field(default="done", init=False)


@dataclass(slots=True)
class Error:
    reason: FailureReason
    error: AssistantMessage
    type: Literal["error"] = field(default="error", init=False)


type AssistantMessageEvent = (
    Start
    | TextStart
    | TextDelta
    | TextEnd
    | ThinkingStart
    | ThinkingDelta
    | ThinkingEnd
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | Done
    | Error
)
