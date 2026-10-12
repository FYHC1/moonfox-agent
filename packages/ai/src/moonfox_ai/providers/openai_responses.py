"""Stateless OpenAI Responses adapter with an injected HTTP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
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
)
from ._responses_stream import REASONING_SIGNATURE, consume_responses, reasoning_item
from ._sse import InvalidResponse, SSEFrame
from .base import StreamOptions
from .http import HTTPTransport


class OpenAIResponsesProvider(RemoteProvider):
    """Use /responses, not Chat Completions; no server-side conversation is required."""

    __slots__ = ("_supports_reasoning",)
    _supports_reasoning: bool

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
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
        if not isinstance(supports_reasoning, bool):
            raise TypeError("Capabilities must be boolean")
        object.__setattr__(self, "_supports_reasoning", supports_reasoning)

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        options = checked_options(options)
        validate_context(model, context, self._supports_tools)
        payload = self._payload(model, context, options)
        return self._start("responses", model, options, payload, self._consume, responses=True)

    def _payload(
        self, model: Model, context: TranscriptContext, options: StreamOptions
    ) -> dict[str, Any]:
        number(options.temperature, 0, 2, "temperature")
        number(options.top_p, 0, 1, "top_p")
        if any(
            value is not None
            for value in (
                options.frequency_penalty,
                options.presence_penalty,
                options.seed,
                options.stop,
            )
        ):
            raise ValueError("Responses does not support penalties, seed or stop sequences")
        if options.reasoning not in (None, "off", "low", "medium", "high"):
            raise ValueError("Unsupported reasoning level")
        if options.reasoning not in (None, "off") and not self._supports_reasoning:
            raise ValueError("Provider does not support reasoning requests")
        items: list[dict[str, Any]] = []
        for message in context.messages:
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, UserMessage):
                items.append(
                    {
                        "role": "user",
                        "content": message.content
                        if isinstance(message.content, str)
                        else self._input_blocks(message.content),
                    }
                )
            elif isinstance(message, AssistantMessage):
                same_model = (message.api, message.provider, message.model) == (
                    "openai-responses",
                    model.provider,
                    model.id,
                )
                for block in message.content:
                    if isinstance(block, TextContent):
                        # An easy input message needs no server-owned message ID.
                        if block.text:
                            items.append({"role": "assistant", "content": block.text})
                    elif isinstance(block, ToolCall):
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": block.id,
                                "name": block.name,
                                "arguments": request_json(block.arguments),
                            }
                        )
                    elif isinstance(block, ThinkingContent):
                        signature = block.thinking_signature
                        if same_model and signature and signature.startswith(REASONING_SIGNATURE):
                            try:
                                item = reasoning_item(
                                    json_object(signature[len(REASONING_SIGNATURE) :])
                                )
                            except InvalidResponse:
                                raise ValueError("Invalid Responses reasoning signature") from None
                            if item.get("encrypted_content"):
                                items.append(item)
                    else:
                        raise ValueError("Unsupported assistant content")
            elif isinstance(message, ToolResultMessage):
                output: str | list[dict[str, Any]]
                if all(isinstance(block, TextContent) for block in message.content):
                    output = "\n".join(
                        block.text for block in message.content if isinstance(block, TextContent)
                    )
                else:
                    output = self._input_blocks(message.content)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": output,
                    }
                )
        payload: dict[str, Any] = {
            "model": model.id,
            "input": items,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": max_tokens(model, options),
        }
        prompt = get_current_system_prompt(context.messages)
        if prompt:
            payload["instructions"] = prompt
        for name, value in (("temperature", options.temperature), ("top_p", options.top_p)):
            if value is not None:
                payload[name] = value
        if options.reasoning not in (None, "off"):
            payload["reasoning"] = {"effort": options.reasoning}
        tools = get_current_tools(context.messages)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": deepcopy(tool.parameters),
                    # Do not let Responses silently require every optional schema property.
                    "strict": False,
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _input_blocks(self, blocks: list[TextContent | ImageContent]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, TextContent):
                result.append({"type": "input_text", "text": block.text})
            elif isinstance(block, ImageContent):
                if not self._supports_images:
                    raise ValueError("Provider does not support images")
                if block.mime_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                    raise ValueError("Unsupported image media type")
                result.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{block.mime_type};base64,{block.data}",
                    }
                )
            else:
                raise ValueError("Unsupported input content")
        return result

    async def _consume(self, frames: AsyncIterator[SSEFrame], state: MessageState) -> None:
        await consume_responses(frames, state, supports_tools=self._supports_tools)
