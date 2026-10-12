"""Allowlisted public projections; never serialize a core message/event wholesale."""

from collections.abc import Sequence
from typing import Any

from moonfox_agent.api import AgentEvent, AgentMessage, MessageEnd, MessageStart, MessageUpdate
from moonfox_ai.api import (
    AssistantMessage,
    ContentBlock,
    ImageContent,
    TextContent,
    TextDelta,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

from .schema import MessageView


def _text(content: str | Sequence[ContentBlock]) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part.text if isinstance(part, TextContent) else "[图片]"
        for part in content
        if isinstance(part, (TextContent, ImageContent))
    )


def project_message(
    message_id: str, message: AgentMessage, *, status: str = "complete"
) -> dict[str, Any] | None:
    """Expose display text and tool identity, not reasoning, arguments or audit payloads."""
    if not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage)):
        return None
    text = _text(message.content)
    calls: list[dict[str, Any]] = []
    tool_name: str | None = None
    is_error = False
    if isinstance(message, AssistantMessage):
        is_error = message.stop_reason == "error" or message.error_message is not None
        if is_error:
            text, status = "生成回复失败。", "error"
        elif message.stop_reason == "aborted":
            status = "aborted"
        calls = [
            {"id": part.id, "name": part.name}
            for part in message.content
            if isinstance(part, ToolCall)
        ]
    elif isinstance(message, ToolResultMessage):
        tool_name, is_error = message.tool_name, message.is_error
        if is_error:
            text, status = "工具执行失败。", "error"
    return MessageView(
        id=message_id,
        role=message.role,
        text=text,
        timestamp=message.timestamp,
        status=status,
        tool_calls=calls,
        tool_name=tool_name,
        is_error=is_error,
    ).model_dump(mode="json")


def project_agent_event(event: AgentEvent) -> tuple[str, dict[str, Any]] | None:
    """Preserve the stable core message id while exposing only safe streaming text."""
    if isinstance(event, MessageUpdate):
        delta = event.assistant_message_event
        if isinstance(delta, TextDelta):
            return "text_delta", {"message_id": event.message_id, "delta": delta.delta}
        return None
    if isinstance(event, (MessageStart, MessageEnd)):
        status = (
            "streaming"
            if isinstance(event, MessageStart) and isinstance(event.message, AssistantMessage)
            else "complete"
        )
        message = project_message(event.message_id, event.message, status=status)
        if message is not None:
            return event.type, {"message": message}
    return None
