"""Append-only facts. Unknown extension payloads remain JSON data, never executable code."""

import json
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal, cast

from pydantic import Field, TypeAdapter

from moonfox_ai.api import Message, Model, Usage
from moonfox_app.extension_api import CustomEntryDraft, SessionBinding

type StoredMessage = Annotated[Message, Field(discriminator="role")]


@dataclass(frozen=True, slots=True)
class EntryBase:
    id: str
    parent_id: str | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class HeaderEntry(EntryBase):
    binding: SessionBinding
    model: Model
    thinking_level: str = "off"
    version: Literal[1] = 1
    type: Literal["header"] = field(default="header", init=False)


@dataclass(frozen=True, slots=True)
class MessageEntry(EntryBase):
    message: StoredMessage
    type: Literal["message"] = field(default="message", init=False)


@dataclass(frozen=True, slots=True)
class ModelChangeEntry(EntryBase):
    model: Model
    type: Literal["model_change"] = field(default="model_change", init=False)


@dataclass(frozen=True, slots=True)
class ThinkingLevelChangeEntry(EntryBase):
    level: str
    type: Literal["thinking_level_change"] = field(default="thinking_level_change", init=False)


@dataclass(frozen=True, slots=True)
class CompactionEntry(EntryBase):
    summary: str
    first_kept_entry_id: str | None
    usage: Usage | None = None
    type: Literal["compaction"] = field(default="compaction", init=False)


@dataclass(frozen=True, slots=True)
class CustomEntry(EntryBase):
    custom_type: str
    data: Any = None
    context_required: bool = False
    type: Literal["custom"] = field(default="custom", init=False)


@dataclass(frozen=True, slots=True)
class ContextEditEntry(EntryBase):
    target_id: str
    replacement: StoredMessage
    type: Literal["context_edit"] = field(default="context_edit", init=False)


type SessionEntry = (
    HeaderEntry
    | MessageEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | CompactionEntry
    | CustomEntry
    | ContextEditEntry
)
ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(
    cast(Any, Annotated[SessionEntry, Field(discriminator="type")])
)


@dataclass(frozen=True, slots=True)
class MessageDraft:
    message: Message
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelChangeDraft:
    model: Model


@dataclass(frozen=True, slots=True)
class ThinkingLevelDraft:
    level: str


@dataclass(frozen=True, slots=True)
class CompactionDraft:
    summary: str
    first_kept_entry_id: str | None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ContextEditDraft:
    target_id: str
    replacement: Message


type EntryDraft = (
    MessageDraft
    | ModelChangeDraft
    | ThinkingLevelDraft
    | CompactionDraft
    | CustomEntryDraft
    | ContextEditDraft
)


def entry_data(entry: SessionEntry) -> dict[str, Any]:
    return asdict(entry)


def parse_entry(data: dict[str, Any]) -> SessionEntry:
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    entry = ENTRY_ADAPTER.validate_json(encoded, strict=True)
    # Dataclass init=False literals and unknown fields must not be silently discarded.
    canonical = json.loads(json.dumps(entry_data(entry), ensure_ascii=False, allow_nan=False))
    if canonical != data:
        raise ValueError("Noncanonical entry: unknown fields, message roles or content tags")
    return entry
