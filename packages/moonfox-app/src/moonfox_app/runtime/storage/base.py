"""Synchronous, single-writer transaction port. No I/O occurs on import."""

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from ..session.entries import HeaderEntry, SessionEntry, entry_data, parse_entry


class StoreError(RuntimeError):
    pass


class StoreCorruptionError(StoreError):
    pass


class StoreConflictError(StoreError):
    pass


class StoreBusyError(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    entries: tuple[SessionEntry, ...] = ()
    leaf_id: str | None = None
    revision: int = 0


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def validate_entry(entry: SessionEntry) -> SessionEntry:
    """JSON roundtrip rejects nonportable tool details and detaches all nested data."""
    normalized = parse_entry(json.loads(json_bytes(entry_data(entry))))
    if not normalized.id or normalized.parent_id == normalized.id or normalized.timestamp < 0:
        raise ValueError("Invalid entry identity, parent or timestamp")
    return normalized


def apply_batch(
    current: StoreSnapshot,
    entries: Sequence[SessionEntry],
    *,
    leaf_id: str,
    expected_revision: int,
) -> StoreSnapshot:
    if current.revision != expected_revision:
        raise StoreConflictError("Session revision changed; rebuild the preview")
    known = {entry.id for entry in current.entries}
    additions: list[SessionEntry] = []
    for entry in entries:
        item = validate_entry(entry)
        if item.id in known:
            raise ValueError(f"Duplicate entry ID: {item.id}")
        if not known:
            if not isinstance(item, HeaderEntry) or item.parent_id is not None:
                raise ValueError("First entry must be a root header")
        elif isinstance(item, HeaderEntry) or item.parent_id not in known:
            raise ValueError("Duplicate header or orphan parent")
        known.add(item.id)
        additions.append(item)
    if leaf_id not in known:
        raise ValueError("Active leaf must refer to an existing entry")
    return StoreSnapshot((*deepcopy(current.entries), *additions), leaf_id, current.revision + 1)


class SessionStore(Protocol):
    def snapshot(self) -> StoreSnapshot: ...

    def commit(
        self,
        entries: Sequence[SessionEntry],
        *,
        leaf_id: str,
        expected_revision: int,
    ) -> StoreSnapshot: ...

    def close(self) -> None: ...


class InMemorySessionStore:
    """The same CAS/validation/atomicity rules as JSONL, without persistence."""

    def __init__(self) -> None:
        self._snapshot = StoreSnapshot()
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise StoreError("Store is closed")

    def snapshot(self) -> StoreSnapshot:
        self._require_open()
        return deepcopy(self._snapshot)

    def commit(
        self,
        entries: Sequence[SessionEntry],
        *,
        leaf_id: str,
        expected_revision: int,
    ) -> StoreSnapshot:
        self._require_open()
        candidate = apply_batch(
            self._snapshot, entries, leaf_id=leaf_id, expected_revision=expected_revision
        )
        self._snapshot = candidate
        return self.snapshot()

    def close(self) -> None:
        self._closed = True
