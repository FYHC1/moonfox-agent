"""Anthropic messages protocol adapter using only injected, asynchronous HTTP I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..stream import AssistantMessageEventStream
from ..transcript import get_current_system_prompt, get_current_tools
from ..types import (
    AssistantMessage,
    ImageContent,
    Model,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UserMessage,
)
from ._remote import (
    MessageState,
    RemoteProvider,
    checked_options,
    json_object,
    max_tokens,
    number,
    validate_context,
    wire_int,
    wire_object,
    wire_string,
)
from ._sse import InvalidResponse, SSEFrame
from .base import StreamOptions
from .http import HTTPTransport


class AnthropicProvider(RemoteProvider):
    """Text, tool and opt-in image requests; historical reasoning is never replayed."""

    __slots__ = ()

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        supports_tools: bool = True,
        supports_images: bool = False,
    ) -> None:
        super().__init__(
            transport,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            supports_tools=supports_tools,
            supports_images=supports_images,
        )

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        options = checked_options(options)
        validate_context(model, context, self._supports_tools)
        payload = self._payload(model, context, options)
        return self._start("messages", model, options, payload, self._consume, anthropic=True)

    def _payload(
        self, model: Model, context: TranscriptContext, options: StreamOptions
    ) -> dict[str, Any]:
        if any(
            value is not None
            for value in (
                options.frequency_penalty,
                options.presence_penalty,
                options.seed,
            )
        ) or options.reasoning not in (None, "off"):
            raise ValueError("Unsupported Anthropic sampling or reasoning option")
        if options.temperature is not None and options.top_p is not None:
            raise ValueError("Anthropic accepts temperature or top_p, not both")
        number(options.temperature, 0, 1, "temperature")
        number(options.top_p, 0, 1, "top_p")
        messages: list[dict[str, Any]] = []
        for message in context.messages:
            if isinstance(message, SystemMessage):
                continue
            role = "user"
            blocks: list[dict[str, Any]] = []
            if isinstance(message, UserMessage):
                blocks = (
                    [{"type": "text", "text": message.content}]
                    if isinstance(message.content, str)
                    else self._input_blocks(message.content)
                )
            elif isinstance(message, ToolResultMessage):
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": self._input_blocks(message.content),
                        "is_error": message.is_error,
                    }
                ]
            elif isinstance(message, AssistantMessage):
                role = "assistant"
                for block in message.content:
                    if isinstance(block, TextContent):
                        blocks.append({"type": "text", "text": block.text})
                    elif isinstance(block, ToolCall):
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": deepcopy(block.arguments),
                            }
                        )
                    elif not isinstance(block, ThinkingContent):
                        raise ValueError("Unsupported assistant content")
            if blocks:
                if messages and messages[-1]["role"] == role:
                    messages[-1]["content"].extend(blocks)
                else:
                    messages.append({"role": role, "content": blocks})
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens(model, options),
        }
        prompt = get_current_system_prompt(context.messages)
        if prompt:
            payload["system"] = prompt
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.top_p is not None:
            payload["top_p"] = options.top_p
        if options.stop is not None:
            payload["stop_sequences"] = list(options.stop)
        tools = get_current_tools(context.messages)
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": deepcopy(tool.parameters),
                }
                for tool in tools
            ]
            payload["tool_choice"] = {"type": "auto"}
        return payload

    def _input_blocks(self, blocks: list[TextContent | ImageContent]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, TextContent):
                result.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                if not self._supports_images:
                    raise ValueError("Provider does not support images")
                if block.mime_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                    raise ValueError("Unsupported image media type")
                result.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.mime_type,
                            "data": block.data,
                        },
                    }
                )
            else:
                raise ValueError("Unsupported input content")
        return result

    async def _consume(self, frames: AsyncIterator[SSEFrame], state: MessageState) -> None:
        started = False
        finished = False
        blocks: dict[int, _Block] = {}
        async for frame in frames:
            data = json_object(frame.data)
            event = wire_string(data.get("type"), nonempty=True)
            if frame.event not in ("message", event):
                raise InvalidResponse()
            if event == "ping":
                continue
            if event == "error":
                raise InvalidResponse()
            if event == "message_start":
                if started:
                    raise InvalidResponse()
                message = wire_object(data.get("message"))
                if message.get("role") != "assistant" or message.get("content", []) != []:
                    raise InvalidResponse()
                state.message.response_id = wire_string(message.get("id"), nonempty=True)
                state.message.response_model = wire_string(message.get("model"), nonempty=True)
                _usage(wire_object(message.get("usage")), state.message.usage)
                started = True
                continue
            if not started:
                raise InvalidResponse()
            if event == "message_stop":
                if not finished or any(not block.stopped for block in blocks.values()):
                    raise InvalidResponse()
                return
            if event == "message_delta":
                if any(not block.stopped for block in blocks.values()):
                    raise InvalidResponse()
                delta = wire_object(data.get("delta"))
                reason = delta.get("stop_reason")
                if reason is not None:
                    if finished or reason not in (
                        "end_turn",
                        "stop_sequence",
                        "max_tokens",
                        "tool_use",
                    ):
                        raise InvalidResponse()
                    has_calls = bool(state.complete_calls)
                    if (reason == "tool_use") != has_calls:
                        raise InvalidResponse()
                    state.message.raw_stop_reason = reason
                    if has_calls:
                        state.message.stop_reason = "tool_use"
                    elif reason == "max_tokens":
                        state.message.stop_reason = "length"
                    else:
                        state.message.stop_reason = "stop"
                    state.message.end_turn = reason in ("end_turn", "stop_sequence")
                    finished = True
                if data.get("usage") is not None:
                    _usage(wire_object(data["usage"]), state.message.usage)
                continue
            if finished:
                raise InvalidResponse()
            index = wire_int(data.get("index"))
            if event == "content_block_start":
                if index != len(blocks) or index >= 1024:
                    raise InvalidResponse()
                block = wire_object(data.get("content_block"))
                kind = block.get("type")
                if kind == "text":
                    content_index = state.text_start(wire_string(block.get("text", "")))
                    blocks[index] = _Block(content_index, "text")
                elif kind == "tool_use":
                    if not self._supports_tools:
                        raise InvalidResponse()
                    call_id = wire_string(block.get("id"), nonempty=True)
                    name = wire_string(block.get("name"), nonempty=True)
                    initial = wire_object(block.get("input"))
                    content_index = state.tool_start(call_id, name)
                    blocks[index] = _Block(content_index, "tool_use", initial=initial)
                elif kind in ("thinking", "redacted_thinking"):
                    content_index = state.thinking_start(
                        wire_string(block.get("thinking", "")) if kind == "thinking" else ""
                    )
                    thinking = state.message.content[content_index]
                    assert isinstance(thinking, ThinkingContent)
                    if block.get("signature") is not None:
                        thinking.thinking_signature = wire_string(block["signature"])
                    thinking.redacted = kind == "redacted_thinking"
                    blocks[index] = _Block(content_index, kind)
                else:
                    raise InvalidResponse()
            elif event in ("content_block_delta", "content_block_stop"):
                if index not in blocks or blocks[index].stopped:
                    raise InvalidResponse()
                live = blocks[index]
                if event == "content_block_stop":
                    if live.kind == "tool_use":
                        if live.arguments and live.initial:
                            raise InvalidResponse()
                        arguments = (
                            json_object("".join(live.arguments)) if live.arguments else live.initial
                        )
                        state.tool_end(live.content_index, arguments)
                    else:
                        state.end(live.content_index)
                    live.stopped = True
                else:
                    delta = wire_object(data.get("delta"))
                    kind = delta.get("type")
                    if kind == "text_delta" and live.kind == "text":
                        state.text_delta(live.content_index, wire_string(delta.get("text")))
                    elif kind == "input_json_delta" and live.kind == "tool_use":
                        part = wire_string(delta.get("partial_json"))
                        live.arguments.append(part)
                        state.tool_delta(live.content_index, part)
                    elif kind == "thinking_delta" and live.kind == "thinking":
                        state.thinking_delta(live.content_index, wire_string(delta.get("thinking")))
                    elif kind == "signature_delta" and live.kind == "thinking":
                        thinking = state.message.content[live.content_index]
                        assert isinstance(thinking, ThinkingContent)
                        thinking.thinking_signature = (
                            thinking.thinking_signature or ""
                        ) + wire_string(delta.get("signature"))
                    else:
                        raise InvalidResponse()
            else:
                raise InvalidResponse()
        raise InvalidResponse()


@dataclass(slots=True)
class _Block:
    content_index: int
    kind: str
    initial: dict[str, Any] = field(default_factory=dict)
    arguments: list[str] = field(default_factory=list)
    stopped: bool = False


def _usage(data: dict[str, Any], usage: Usage) -> None:
    for key, attr in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read"),
        ("cache_creation_input_tokens", "cache_write"),
    ):
        if key in data:
            setattr(usage, attr, wire_int(data[key]))
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
