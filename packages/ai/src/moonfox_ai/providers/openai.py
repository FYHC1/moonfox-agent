"""OpenAI-compatible chat-completions adapter with an injected HTTP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

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
    request_json,
    validate_context,
    wire_int,
    wire_object,
    wire_string,
)
from ._sse import InvalidResponse, SSEFrame
from .base import StreamOptions
from .http import HTTPTransport


class OpenAIProvider(RemoteProvider):
    """Stream the first completion choice; no network client or retries are installed."""

    __slots__ = ("_token_field", "_supports_reasoning")
    _token_field: Literal["max_tokens", "max_completion_tokens"]
    _supports_reasoning: bool

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        supports_tools: bool = True,
        supports_images: bool = False,
        supports_reasoning: bool = False,
    ) -> None:
        super().__init__(
            transport,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            supports_tools=supports_tools,
            supports_images=supports_images,
        )
        if token_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("Unsupported token field")
        if not isinstance(supports_reasoning, bool):
            raise TypeError("Capabilities must be boolean")
        object.__setattr__(self, "_token_field", token_field)
        object.__setattr__(self, "_supports_reasoning", supports_reasoning)

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        options = checked_options(options)
        validate_context(model, context, self._supports_tools)
        payload = self._payload(model, context, options)
        return self._start("chat/completions", model, options, payload, self._consume)

    def _payload(
        self, model: Model, context: TranscriptContext, options: StreamOptions
    ) -> dict[str, Any]:
        number(options.temperature, 0, 2, "temperature")
        number(options.top_p, 0, 1, "top_p")
        number(options.frequency_penalty, -2, 2, "frequency_penalty")
        number(options.presence_penalty, -2, 2, "presence_penalty")
        if options.seed is not None and (
            isinstance(options.seed, bool) or not isinstance(options.seed, int)
        ):
            raise ValueError("seed must be an integer")
        if options.reasoning not in (None, "off", "low", "medium", "high"):
            raise ValueError("Unsupported reasoning level")
        if options.reasoning not in (None, "off") and not self._supports_reasoning:
            raise ValueError("Provider does not support reasoning requests")
        messages: list[dict[str, Any]] = []
        prompt = get_current_system_prompt(context.messages)
        if prompt:
            messages.append({"role": "system", "content": prompt})
        for message in context.messages:
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, UserMessage):
                content: str | list[dict[str, Any]] = (
                    message.content
                    if isinstance(message.content, str)
                    else self._input_blocks(message.content)
                )
                messages.append({"role": "user", "content": content})
            elif isinstance(message, AssistantMessage):
                text: list[str] = []
                calls: list[dict[str, Any]] = []
                for block in message.content:
                    if isinstance(block, TextContent):
                        text.append(block.text)
                    elif isinstance(block, ToolCall):
                        calls.append(
                            {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": request_json(block.arguments),
                                },
                            }
                        )
                    elif not isinstance(block, ThinkingContent):
                        raise ValueError("Unsupported assistant content")
                if text or calls:
                    entry: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
                    if calls:
                        entry["tool_calls"] = calls
                    messages.append(entry)
            elif isinstance(message, ToolResultMessage):
                # Chat-completions tool messages accept text only, even on image models.
                if any(not isinstance(block, TextContent) for block in message.content):
                    raise ValueError("OpenAI tool results support text only")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": "\n".join(
                            block.text
                            for block in message.content
                            if isinstance(block, TextContent)
                        ),
                    }
                )
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            self._token_field: max_tokens(model, options),
        }
        for name, value in (
            ("temperature", options.temperature),
            ("top_p", options.top_p),
            ("frequency_penalty", options.frequency_penalty),
            ("presence_penalty", options.presence_penalty),
            ("seed", options.seed),
        ):
            if value is not None:
                payload[name] = value
        if options.stop is not None:
            payload["stop"] = list(options.stop)
        if options.reasoning not in (None, "off"):
            payload["reasoning_effort"] = options.reasoning
        tools = get_current_tools(context.messages)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": deepcopy(tool.parameters),
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
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
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{block.mime_type};base64,{block.data}",
                        },
                    }
                )
            else:
                raise ValueError("Unsupported input content")
        return result

    async def _consume(self, frames: AsyncIterator[SSEFrame], state: MessageState) -> None:
        text_index: int | None = None
        thinking_index: int | None = None
        calls: dict[int, _Call] = {}
        finished = False
        async for frame in frames:
            if frame.event not in ("message", "completion"):
                raise InvalidResponse()
            if frame.data == "[DONE]":
                if not finished:
                    raise InvalidResponse()
                return
            data = json_object(frame.data)
            if "error" in data:
                raise InvalidResponse()
            _identity(data, state)
            if data.get("usage") is not None:
                _usage(wire_object(data["usage"]), state.message.usage)
            choices = data.get("choices")
            if not isinstance(choices, list):
                raise InvalidResponse()
            if not choices:
                if data.get("usage") is None:
                    raise InvalidResponse()
                continue
            if finished:
                raise InvalidResponse()
            choice = wire_object(choices[0])
            if wire_int(choice.get("index", 0)) != 0:
                raise InvalidResponse()
            delta = wire_object(choice.get("delta"))
            if delta.get("role") not in (None, "assistant"):
                raise InvalidResponse()
            if any(delta.get(key) is not None for key in ("function_call", "audio", "refusal")):
                raise InvalidResponse()
            if delta.get("content") is not None:
                text = wire_string(delta["content"])
                if text_index is None:
                    text_index = state.text_start()
                if text:
                    state.text_delta(text_index, text)
            for key in ("reasoning_content", "thinking"):
                if delta.get(key) is not None:
                    thinking = wire_string(delta[key])
                    if thinking_index is None:
                        thinking_index = state.thinking_start()
                    if thinking:
                        state.thinking_delta(thinking_index, thinking)
            if delta.get("tool_calls") is not None:
                if not self._supports_tools or not isinstance(delta["tool_calls"], list):
                    raise InvalidResponse()
                for raw_call in delta["tool_calls"]:
                    call = wire_object(raw_call)
                    index = wire_int(call.get("index"))
                    if call.get("type") not in (None, "function"):
                        raise InvalidResponse()
                    if index not in calls:
                        if index >= 1024 or len(calls) >= 1024:
                            raise InvalidResponse()
                        calls[index] = _Call(state.tool_start())
                    live = calls[index]
                    block = state.message.content[live.content_index]
                    assert isinstance(block, ToolCall)
                    if call.get("id") is not None:
                        block.id += wire_string(call["id"])
                    if call.get("function") is not None:
                        function = wire_object(call["function"])
                        if function.get("name") is not None:
                            block.name += wire_string(function["name"])
                        if function.get("arguments") is not None:
                            arguments = wire_string(function["arguments"])
                            live.arguments.append(arguments)
                            state.tool_delta(live.content_index, arguments)
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish not in ("stop", "length", "tool_calls"):
                    raise InvalidResponse()
                if (finish == "tool_calls") != bool(calls):
                    raise InvalidResponse()
                if set(calls) != set(range(len(calls))):
                    raise InvalidResponse()
                # Parse only after completion; partial JSON is never presented as arguments.
                for live in calls.values():
                    state.tool_end(live.content_index, json_object("".join(live.arguments)))
                for content_index in (text_index, thinking_index):
                    if content_index is not None:
                        state.end(content_index)
                state.message.raw_stop_reason = finish
                state.message.stop_reason = "tool_use" if calls else finish
                finished = True
        # Both a finish_reason and the explicit sentinel are required.
        raise InvalidResponse()


@dataclass(slots=True)
class _Call:
    content_index: int
    arguments: list[str] = field(default_factory=list)


def _identity(data: dict[str, Any], state: MessageState) -> None:
    for key, attr in (("id", "response_id"), ("model", "response_model")):
        if data.get(key) is None:
            continue
        value = wire_string(data[key], nonempty=True)
        previous = getattr(state.message, attr)
        if previous is not None and previous != value:
            raise InvalidResponse()
        setattr(state.message, attr, value)


def _usage(data: dict[str, Any], usage: Usage) -> None:
    prompt = wire_int(data.get("prompt_tokens", usage.input + usage.cache_read))
    usage.output = wire_int(data.get("completion_tokens", usage.output))
    if data.get("prompt_tokens_details") is not None:
        details = wire_object(data["prompt_tokens_details"])
        usage.cache_read = wire_int(details.get("cached_tokens", 0))
    if usage.cache_read > prompt:
        raise InvalidResponse()
    # Cached tokens are included in prompt_tokens on this wire protocol.
    usage.input = prompt - usage.cache_read
    if data.get("completion_tokens_details") is not None:
        details = wire_object(data["completion_tokens_details"])
        if details.get("reasoning_tokens") is not None:
            usage.reasoning = wire_int(details["reasoning_tokens"])
            if usage.reasoning > usage.output:
                raise InvalidResponse()
    usage.total_tokens = wire_int(data.get("total_tokens", prompt + usage.output))
