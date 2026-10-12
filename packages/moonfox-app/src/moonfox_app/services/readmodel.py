"""Public-text read model. No reasoning, provider errors, internal scope or audit payloads."""

from typing import Any

from moonfox_agent.api import AgentMessage, MessageEnd, MessageStart, MessageUpdate
from moonfox_ai.api import (
    AssistantMessage,
    ImageContent,
    TextContent,
    TextDelta,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from moonfox_app.extension_api import SessionEvent, SessionView


def public_message(
    identity: str, message: AgentMessage, *, status: str = "complete"
) -> dict[str, Any] | None:
    if not isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage)):
        return None
    text = (
        message.content
        if isinstance(message.content, str)
        else "".join(
            block.text
            if isinstance(block, TextContent)
            else "[图片]"
            if isinstance(block, ImageContent)
            else ""
            for block in message.content
        )
    )
    calls: list[dict[str, Any]] = []
    if isinstance(message, AssistantMessage):
        calls = [
            {"id": block.id, "name": block.name}
            for block in message.content
            if isinstance(block, ToolCall)
        ]
        if message.stop_reason in ("error", "aborted"):
            status = message.stop_reason
            text = "运行已停止。" if status == "aborted" else "模型请求失败，请检查宿主诊断。"
    return {
        "id": identity,
        "role": message.role,
        "text": text,
        "timestamp": message.timestamp,
        "status": status,
        "tool_calls": calls,
        "tool_name": message.tool_name if isinstance(message, ToolResultMessage) else None,
        "is_error": message.is_error if isinstance(message, ToolResultMessage) else False,
    }


def view_messages(view: SessionView) -> list[dict[str, Any]]:
    if len(view.messages) != len(view.message_ids):
        raise ValueError("Session view must supply stable message IDs")
    output: list[dict[str, Any]] = []
    for index, (identity, message) in enumerate(zip(view.message_ids, view.messages, strict=True)):
        projected = public_message(identity or f"projection-{view.leaf_id}-{index}", message)
        if projected is not None:
            output.append(projected)
    return output


def session_update(event: SessionEvent) -> tuple[str, dict[str, Any]] | None:
    if event.type != "agent_event":
        return None
    core = event.payload
    if isinstance(core, (MessageStart, MessageEnd)):
        projected = public_message(
            core.message_id,
            core.message,
            status="streaming" if isinstance(core, MessageStart) else "complete",
        )
        if projected is not None:
            return core.type, {"message": projected}
    if isinstance(core, MessageUpdate) and isinstance(core.assistant_message_event, TextDelta):
        return "text_delta", {
            "message_id": core.message_id,
            "delta": core.assistant_message_event.delta,
        }
    return None
