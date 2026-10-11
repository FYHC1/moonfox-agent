"""Deterministic, offline scripted provider with real event and cancellation semantics."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from ..stream import AssistantMessageEventStream
from ..transcript import normalize_context
from ..types import (
    AssistantMessage,
    ContentBlock,
    Context,
    Done,
    Error,
    FailureReason,
    ImageContent,
    Model,
    Start,
    StopReason,
    TextContent,
    TextDelta,
    TextEnd,
    TextStart,
    ThinkingContent,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    TranscriptContext,
    Usage,
    now_ms,
)
from .base import AbortSignal, StreamOptions


class FauxScriptExhausted(AssertionError):
    """The test or caller requested more responses than its script supplied."""


@dataclass(slots=True)
class FauxResponse:
    content: list[ContentBlock]
    stop_reason: StopReason = "stop"
    usage: Usage = field(default_factory=Usage)
    error_message: str | None = None
    delay_ms: int = 0
    chunk_size: int = 16

    def __post_init__(self) -> None:
        if self.stop_reason not in ("stop", "length", "tool_use", "error", "aborted", "deferred"):
            raise ValueError("FauxResponse requires a terminal stop_reason")
        if (
            isinstance(self.delay_ms, bool)
            or not isinstance(self.delay_ms, int)
            or self.delay_ms < 0
        ):
            raise ValueError("delay_ms must be a nonnegative integer")
        if (
            isinstance(self.chunk_size, bool)
            or not isinstance(self.chunk_size, int)
            or self.chunk_size < 1
        ):
            raise ValueError("chunk_size must be a positive integer")
        for block in self.content:
            if not isinstance(block, (TextContent, ThinkingContent, ImageContent, ToolCall)):
                raise TypeError("Unsupported faux content block")
            if isinstance(block, ToolCall):
                json.dumps(block.arguments, allow_nan=False)

    @classmethod
    def text(cls, text: str, **kwargs: Any) -> FauxResponse:
        return cls(content=[TextContent(text)], **kwargs)

    @classmethod
    def tool_call(
        cls, name: str, arguments: dict[str, Any], call_id: str = "call_1", **kwargs: Any
    ) -> FauxResponse:
        kwargs.setdefault("stop_reason", "tool_use")
        return cls(content=[ToolCall(call_id, name, arguments)], **kwargs)

    @classmethod
    def error(
        cls, message: str, stop_reason: FailureReason = "error", **kwargs: Any
    ) -> FauxResponse:
        return cls(content=[], stop_reason=stop_reason, error_message=message, **kwargs)

    def to_stream(
        self, model: Model, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        stream = AssistantMessageEventStream()
        script = deepcopy(self)
        signal = options.signal if options is not None else None
        stream.start(script._produce(stream, model, signal))
        return stream

    async def _produce(
        self, stream: AssistantMessageEventStream, model: Model, signal: AbortSignal | None
    ) -> None:
        message = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=deepcopy(self.usage),
            stop_reason="pending",
            timestamp=now_ms(),
        )

        def aborted() -> bool:
            if signal is None or not signal.aborted:
                return False
            message.stop_reason = "aborted"
            message.error_message = "Operation aborted"
            stream.push(Error("aborted", message))
            return True

        try:
            if aborted():
                return
            if not self.content and self.stop_reason in ("error", "aborted"):
                await _pause(self.delay_ms, signal)
                if not aborted():
                    message.stop_reason = self.stop_reason
                    message.error_message = self.error_message
                    stream.push(Error(self.stop_reason, message))
                return
            stream.push(Start(message))
            await asyncio.sleep(0)
            for index, block in enumerate(self.content):
                if aborted():
                    return
                if isinstance(block, ImageContent):
                    message.content.append(deepcopy(block))
                    continue
                if isinstance(block, TextContent):
                    live_text = TextContent("", text_signature=block.text_signature)
                    message.content.append(live_text)
                    stream.push(TextStart(index, message))
                    for chunk in _chunks(block.text, self.chunk_size):
                        await _pause(self.delay_ms, signal)
                        if aborted():
                            return
                        live_text.text += chunk
                        stream.push(TextDelta(index, chunk, message))
                    stream.push(TextEnd(index, block.text, message))
                elif isinstance(block, ThinkingContent):
                    live_thinking = ThinkingContent(
                        "", thinking_signature=block.thinking_signature, redacted=block.redacted
                    )
                    message.content.append(live_thinking)
                    stream.push(ThinkingStart(index, message))
                    for chunk in _chunks(block.thinking, self.chunk_size):
                        await _pause(self.delay_ms, signal)
                        if aborted():
                            return
                        live_thinking.thinking += chunk
                        stream.push(ThinkingDelta(index, chunk, message))
                    stream.push(ThinkingEnd(index, block.thinking, message))
                elif isinstance(block, ToolCall):
                    live_call = ToolCall(block.id, block.name, {}, block.thought_signature)
                    message.content.append(live_call)
                    stream.push(ToolCallStart(index, message))
                    payload = json.dumps(block.arguments, ensure_ascii=False, allow_nan=False)
                    chunks = list(_chunks(payload, self.chunk_size))
                    for chunk_index, chunk in enumerate(chunks):
                        await _pause(self.delay_ms, signal)
                        if aborted():
                            return
                        if chunk_index == len(chunks) - 1:
                            live_call.arguments = deepcopy(block.arguments)
                        stream.push(ToolCallDelta(index, chunk, message))
                    stream.push(ToolCallEnd(index, live_call, message))
                await asyncio.sleep(0)
            if aborted():
                return
            message.stop_reason = self.stop_reason
            message.error_message = self.error_message
            if self.stop_reason in ("error", "aborted"):
                stream.push(Error(self.stop_reason, message))
            elif self.stop_reason in ("stop", "length", "tool_use", "deferred"):
                stream.push(Done(self.stop_reason, message))
        except asyncio.CancelledError:
            message.stop_reason = "aborted"
            message.error_message = "Stream closed"
            stream.push(Error("aborted", message))
            raise


def _chunks(text: str, size: int) -> Sequence[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


async def _pause(delay_ms: int, signal: AbortSignal | None) -> None:
    if not delay_ms:
        await asyncio.sleep(0)
    elif signal is None:
        await asyncio.sleep(delay_ms / 1000)
    else:
        try:
            async with asyncio.timeout(delay_ms / 1000):
                await signal.wait()
        except TimeoutError:
            pass


class FauxProvider:
    """Consume one response per call; record detached model/context/options snapshots.

    The signal and callbacks in recorded options intentionally retain their identity.
    Script exhaustion is a caller bug, not a simulated operational provider failure.
    """

    def __init__(self, script: Sequence[FauxResponse]) -> None:
        self._script = deque(deepcopy(list(script)))
        self.requests: list[tuple[Model, TranscriptContext, StreamOptions | None]] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        if not isinstance(context, TranscriptContext):
            raise TypeError("FauxProvider requires normalized TranscriptContext")
        if not self._script:
            raise FauxScriptExhausted("FauxProvider script exhausted")
        snapshot = normalize_context(Context(messages=context.messages))
        recorded_options = replace(options, headers=deepcopy(options.headers)) if options else None
        self.requests.append((model, snapshot, recorded_options))
        return self._script.popleft().to_stream(model, options)
