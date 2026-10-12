"""Pure branch projection. Original facts are retained, even after edits/compaction."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from moonfox_agent.api import validate_tool_pairing
from moonfox_ai.api import (
    AssistantMessage,
    Message,
    Model,
    SystemMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    get_current_system_message,
)
from moonfox_app.extension_api import CustomEntryCodec, CustomEntryDraft, SessionBinding

from .entries import (
    CompactionEntry,
    ContextEditEntry,
    CustomEntry,
    HeaderEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    ThinkingLevelChangeEntry,
)


class ProjectionError(ValueError):
    pass


class IncompatibleContextError(ProjectionError):
    pass


@dataclass(frozen=True, slots=True)
class SessionProjection:
    binding: SessionBinding
    entries: tuple[SessionEntry, ...]
    messages: tuple[Message, ...]
    message_entry_ids: tuple[str | None, ...]
    leaf_id: str
    model: Model
    thinking_level: str
    pending_calls: tuple[ToolCall, ...] = ()


def walk_branch(entries: Sequence[SessionEntry], leaf_id: str) -> tuple[SessionEntry, ...]:
    indexed = {entry.id: entry for entry in entries}
    if len(indexed) != len(entries):
        raise ProjectionError("Duplicate entry IDs")
    result: list[SessionEntry] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current not in indexed or current in seen:
            raise ProjectionError("Orphan parent or cyclic branch")
        seen.add(current)
        item = indexed[current]
        result.append(item)
        current = item.parent_id
    result.reverse()
    if not result or not isinstance(result[0], HeaderEntry):
        raise ProjectionError("Branch must start with a header")
    return tuple(deepcopy(result))


def pending_tool_calls(messages: Sequence[Message]) -> tuple[ToolCall, ...]:
    """Only missing results at the tail are recoverable; other malformed history fails."""
    pending: dict[str, ToolCall] = {}
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, ToolResultMessage):
            call = pending.get(message.tool_call_id)
            if call is None or call.name != message.tool_name:
                raise ProjectionError("Orphan, duplicate or mismatched tool result")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise ProjectionError("Unpaired tool calls in the middle of history")
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall):
                    if not block.id or block.id in pending:
                        raise ProjectionError("Empty or duplicate tool call IDs")
                    pending[block.id] = block
    return tuple(deepcopy(list(pending.values())))


def build_projection(
    entries: Sequence[SessionEntry],
    leaf_id: str,
    *,
    codecs: Mapping[str, CustomEntryCodec] | None = None,
    allow_pending: bool = False,
) -> SessionProjection:
    branch = walk_branch(entries, leaf_id)
    header = branch[0]
    assert isinstance(header, HeaderEntry)
    model, thinking = header.model, header.thinking_level
    # Edits must target an earlier message on this branch, never a hidden sibling.
    earlier: dict[str, MessageEntry] = {}
    replacements: dict[str, Message] = {}
    for entry in branch:
        if isinstance(entry, MessageEntry):
            earlier[entry.id] = entry
        elif isinstance(entry, ContextEditEntry):
            target = earlier.get(entry.target_id)
            if target is None or isinstance(target.message, SystemMessage):
                raise ProjectionError("Context edit must target an earlier conversation message")
            if target.message.role != entry.replacement.role:
                raise ProjectionError("Context edit cannot change a message role")
            replacements[entry.target_id] = entry.replacement

    pairs: list[tuple[str | None, Message]] = []
    for entry in branch:
        if isinstance(entry, MessageEntry):
            pairs.append((entry.id, deepcopy(replacements.get(entry.id, entry.message))))
        elif isinstance(entry, ModelChangeEntry):
            model = entry.model
        elif isinstance(entry, ThinkingLevelChangeEntry):
            thinking = entry.level
        elif isinstance(entry, CustomEntry) and entry.context_required:
            codec = (codecs or {}).get(entry.custom_type)
            if codec is None:
                raise IncompatibleContextError(f"Missing context codec: {entry.custom_type}")
            converted = codec(
                CustomEntryDraft(entry.custom_type, deepcopy(entry.data), True), entry.timestamp
            )
            if (
                not isinstance(converted, list)
                or not converted
                or any(not isinstance(message, UserMessage) for message in converted)
            ):
                raise IncompatibleContextError("Custom context codec must return user references")
            pairs.extend((entry.id, deepcopy(message)) for message in converted)
        elif isinstance(entry, CompactionEntry):
            if not entry.summary.strip():
                raise ProjectionError("Compaction summary must not be empty")
            validate_tool_pairing([message for _, message in pairs])
            start = len(pairs)
            if entry.first_kept_entry_id is not None:
                matches = [
                    index
                    for index, (identity, message) in enumerate(pairs)
                    if identity == entry.first_kept_entry_id
                    and not isinstance(message, SystemMessage)
                ]
                if not matches:
                    raise ProjectionError("Compaction cut must name a currently visible message")
                start = matches[0]
            baseline = get_current_system_message([message for _, message in pairs])
            kept = [
                (identity, message)
                for identity, message in pairs[start:]
                if not isinstance(message, SystemMessage)
            ]
            # A split tool batch is not a valid summary boundary.
            validate_tool_pairing([message for _, message in kept])
            pairs = [] if baseline is None else [(None, baseline)]
            pairs.append(
                (
                    entry.id,
                    UserMessage(
                        f"<compaction_summary>\n{entry.summary}\n</compaction_summary>",
                        entry.timestamp,
                    ),
                )
            )
            pairs.extend(kept)
    messages = tuple(message for _, message in pairs)
    pending = pending_tool_calls(messages)
    if pending and not allow_pending:
        raise ProjectionError("Unpaired tool calls at projection tail")
    return SessionProjection(
        header.binding,
        branch,
        messages,
        tuple(identity for identity, _ in pairs),
        leaf_id,
        model,
        thinking,
        pending,
    )
