"""Pure replay operations; the transcript, not a mutable prompt field, is authoritative."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from .types import (
    _BRAND,
    AssistantMessage,
    Context,
    HasToolDeclaration,
    Message,
    SystemMessage,
    ToolDeclaration,
    ToolResultMessage,
    TranscriptContext,
    UserMessage,
    now_ms,
)


@dataclass(slots=True)
class ToolStateChanges:
    tools_added: list[ToolDeclaration]
    tools_removed: list[str]


def create_initial_system_message(
    system_prompt: str | None,
    tools: list[ToolDeclaration] | None,
) -> SystemMessage | None:
    if not system_prompt and not tools:
        return None
    return SystemMessage(system_prompt or "", now_ms(), tools_added=deepcopy(tools))


def normalize_context(context: Context) -> TranscriptContext:
    """Fold shorthand into a leading system entry and detach all mutable input data."""
    if not isinstance(context, Context):
        raise TypeError("normalize_context requires Context")
    if any(
        not isinstance(message, (SystemMessage, UserMessage, AssistantMessage, ToolResultMessage))
        for message in context.messages
    ):
        raise TypeError("Context contains a non-model message; convert it before normalization")
    messages = deepcopy(context.messages)
    head = create_initial_system_message(context.system_prompt, context.tools)
    if head is not None:
        messages.insert(0, head)
    return TranscriptContext(messages, _token=_BRAND)


def get_current_system_message(messages: Sequence[object]) -> SystemMessage | None:
    """Return a replayable baseline, keeping named sections separate from appended text.

    Within one entry, removals precede additions, so replacing a tool by name works.
    Dict insertion order preserves the order of existing sections and declarations.
    Unknown non-system messages are irrelevant to this replay, not silently converted.
    """
    content: list[str] = []
    sections: dict[str, str | None] = {}
    tools: dict[str, ToolDeclaration] = {}
    timestamp: int | None = None
    for message in messages:
        if not isinstance(message, SystemMessage):
            continue
        timestamp = message.timestamp
        text = (
            message.content
            if isinstance(message.content, str)
            else "\n".join(block.text for block in message.content)
        )
        if text:
            content.append(text)
        for name, value in (message.sections or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
        for name in message.tools_removed or []:
            tools.pop(name, None)
        for tool in message.tools_added or []:
            tools[tool.name] = deepcopy(tool)
    if timestamp is None:
        return None
    return SystemMessage(
        content="\n\n".join(content),
        timestamp=timestamp,
        sections=sections or None,
        tools_added=list(tools.values()) or None,
    )


def get_current_system_prompt(messages: Sequence[object]) -> str:
    current = get_current_system_message(messages)
    if current is None:
        return ""
    assert isinstance(current.content, str)
    parts = [current.content, *(value for value in (current.sections or {}).values() if value)]
    return "\n\n".join(part for part in parts if part)


def get_current_tools(messages: Sequence[object]) -> list[ToolDeclaration]:
    current = get_current_system_message(messages)
    return current.tools_added or [] if current is not None else []


def get_tool_state_changes(
    declared: Sequence[ToolDeclaration], executable: Sequence[ToolDeclaration]
) -> ToolStateChanges:
    old = {tool.name: tool for tool in declared}
    new = {tool.name: tool for tool in executable}
    if len(old) != len(declared) or len(new) != len(executable):
        raise ValueError("Tool declaration names must be unique")
    return ToolStateChanges(
        tools_added=[deepcopy(tool) for name, tool in new.items() if old.get(name) != tool],
        tools_removed=[name for name in old if name not in new],
    )


def collapse_system_messages(messages: Sequence[Message]) -> list[Message]:
    """Move the replayed system baseline to the front, preserving other message order."""
    current = get_current_system_message(messages)
    rest: list[Message] = [
        deepcopy(message) for message in messages if not isinstance(message, SystemMessage)
    ]
    return [current, *rest] if current is not None else rest


def to_tool_declaration(tool: HasToolDeclaration) -> ToolDeclaration:
    """Structural protocol avoids importing a higher-layer executable tool type."""
    return deepcopy(tool.declaration())
