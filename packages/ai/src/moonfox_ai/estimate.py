"""Heuristic token estimates, not a tokenizer or a billing guarantee."""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .types import (
    AssistantMessage,
    ImageContent,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)

CHARS_PER_TOKEN = 3.5
ESTIMATED_IMAGE_CHARS = 4800


@dataclass(frozen=True, slots=True)
class ContextUsageEstimate:
    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_message_tokens(message: Message) -> int:
    if isinstance(message.content, str):
        characters = len(message.content)
    else:
        characters = 0
        for block in message.content:
            if isinstance(block, TextContent):
                characters += len(block.text)
            elif isinstance(block, ThinkingContent):
                characters += len(block.thinking)
            elif isinstance(block, ImageContent):
                characters += ESTIMATED_IMAGE_CHARS
            elif isinstance(block, ToolCall):
                characters += len(block.name) + len(json.dumps(block.arguments, ensure_ascii=False))
    if isinstance(message, SystemMessage):
        characters += sum(len(value or "") for value in (message.sections or {}).values())
        for tool in message.tools_added or []:
            characters += len(tool.name) + len(tool.description)
            characters += len(json.dumps(tool.parameters, ensure_ascii=False))
        characters += sum(len(name) for name in message.tools_removed or [])
    # Account for message framing. Details, audit data, and signatures are not tokenized here.
    return 4 + math.ceil(characters / CHARS_PER_TOKEN)


def calculate_context_tokens(usage: Usage) -> int:
    return max(0, usage.effective_total)


def estimate_context_tokens(messages: Sequence[Message]) -> ContextUsageEstimate:
    last_index = None
    usage_tokens = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AssistantMessage) and message.stop_reason not in (
            "pending",
            "error",
            "aborted",
        ):
            count = calculate_context_tokens(message.usage)
            if count:
                last_index, usage_tokens = index, count
                break
    trailing = sum(
        estimate_message_tokens(message)
        for message in messages[(last_index + 1 if last_index is not None else 0) :]
    )
    return ContextUsageEstimate(usage_tokens + trailing, usage_tokens, trailing, last_index)
