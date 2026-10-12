"""Prepare, preview, validate and atomically commit facts over a replaceable store."""

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from moonfox_ai.api import Model, SystemMessage, TextContent, ToolResultMessage, now_ms
from moonfox_app.extension_api import CustomEntryCodec, CustomEntryDraft, SessionBinding

from ..storage.base import SessionStore, StoreConflictError, StoreSnapshot, apply_batch
from .entries import (
    CompactionDraft,
    CompactionEntry,
    ContextEditDraft,
    ContextEditEntry,
    CustomEntry,
    EntryDraft,
    HeaderEntry,
    MessageDraft,
    MessageEntry,
    ModelChangeDraft,
    ModelChangeEntry,
    SessionEntry,
    ThinkingLevelChangeEntry,
    ThinkingLevelDraft,
)
from .projection import SessionProjection, build_projection


@dataclass(frozen=True, slots=True)
class SessionPreview:
    revision: int
    entries: tuple[SessionEntry, ...]
    projection: SessionProjection
    allow_pending: bool = False


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        *,
        binding: SessionBinding,
        codecs: Mapping[str, CustomEntryCodec] | None = None,
    ) -> None:
        self._store = store
        self._codecs = dict(codecs or {})
        snapshot = store.snapshot()
        if not snapshot.entries or not isinstance(snapshot.entries[0], HeaderEntry):
            raise ValueError("Store does not contain a session header")
        if snapshot.entries[0].binding != binding:
            raise PermissionError("Stored session binding does not match the supplied scope")
        self._binding = binding
        self.projection(allow_pending=True)

    @property
    def binding(self) -> SessionBinding:
        return self._binding

    @classmethod
    def create(
        cls,
        store: SessionStore,
        *,
        binding: SessionBinding,
        model: Model,
        system_prompt: str = "",
        thinking_level: str = "off",
        codecs: Mapping[str, CustomEntryCodec] | None = None,
    ) -> "SessionManager":
        if store.snapshot().entries:
            raise ValueError("Cannot initialize a nonempty store")
        header = HeaderEntry(uuid4().hex, None, now_ms(), binding, model, thinking_level)
        initial: list[SessionEntry] = [header]
        if system_prompt:
            initial.append(
                MessageEntry(
                    uuid4().hex, header.id, now_ms(), SystemMessage(system_prompt, now_ms())
                )
            )
        store.commit(initial, leaf_id=initial[-1].id, expected_revision=0)
        return cls(store, binding=binding, codecs=codecs)

    def snapshot(self) -> StoreSnapshot:
        return self._store.snapshot()

    def _project(self, snapshot: StoreSnapshot, *, allow_pending: bool) -> SessionProjection:
        if snapshot.leaf_id is None:
            raise ValueError("Session has no active leaf")
        return build_projection(
            snapshot.entries, snapshot.leaf_id, codecs=self._codecs, allow_pending=allow_pending
        )

    def projection(self, *, allow_pending: bool = False) -> SessionProjection:
        return self._project(self.snapshot(), allow_pending=allow_pending)

    @staticmethod
    def _materialize(draft: EntryDraft, parent: str) -> SessionEntry:
        identity, timestamp = uuid4().hex, now_ms()
        if isinstance(draft, MessageDraft):
            return MessageEntry(
                draft.message_id or identity, parent, timestamp, deepcopy(draft.message)
            )
        if isinstance(draft, ModelChangeDraft):
            return ModelChangeEntry(identity, parent, timestamp, draft.model)
        if isinstance(draft, ThinkingLevelDraft):
            if not draft.level:
                raise ValueError("Thinking level must be nonempty")
            return ThinkingLevelChangeEntry(identity, parent, timestamp, draft.level)
        if isinstance(draft, CompactionDraft):
            return CompactionEntry(
                identity,
                parent,
                timestamp,
                draft.summary,
                draft.first_kept_entry_id,
                deepcopy(draft.usage),
            )
        if isinstance(draft, ContextEditDraft):
            return ContextEditEntry(
                identity, parent, timestamp, draft.target_id, deepcopy(draft.replacement)
            )
        if isinstance(draft, CustomEntryDraft):
            if not re.fullmatch(r"[a-z][a-z0-9_-]*\.[a-zA-Z0-9_.-]+", draft.custom_type):
                raise ValueError("Custom entry types must be namespaced")
            return CustomEntry(
                identity,
                parent,
                timestamp,
                draft.custom_type,
                deepcopy(draft.data),
                draft.context_required,
            )
        raise TypeError("Unknown entry draft")

    def preview(
        self, drafts: Sequence[EntryDraft], *, allow_pending: bool = False
    ) -> SessionPreview:
        snapshot = self.snapshot()
        assert snapshot.leaf_id is not None
        parent = snapshot.leaf_id
        entries: list[SessionEntry] = []
        for draft in drafts:
            entry = self._materialize(draft, parent)
            entries.append(entry)
            parent = entry.id
        candidate = apply_batch(
            snapshot, entries, leaf_id=parent, expected_revision=snapshot.revision
        )
        projection = self._project(candidate, allow_pending=allow_pending)
        return SessionPreview(
            snapshot.revision, tuple(deepcopy(entries)), projection, allow_pending
        )

    def commit_preview(self, preview: SessionPreview) -> SessionProjection:
        snapshot = self.snapshot()
        if snapshot.revision != preview.revision:
            raise StoreConflictError("Preview is stale")
        # Never trust mutable nested values or a caller-modified projection in a preview.
        leaf = preview.entries[-1].id if preview.entries else snapshot.leaf_id
        assert leaf is not None
        candidate = apply_batch(
            snapshot, preview.entries, leaf_id=leaf, expected_revision=preview.revision
        )
        projection = self._project(candidate, allow_pending=preview.allow_pending)
        if preview.entries:
            self._store.commit(preview.entries, leaf_id=leaf, expected_revision=preview.revision)
        return projection

    def commit(
        self, drafts: Sequence[EntryDraft], *, allow_pending: bool = False
    ) -> SessionProjection:
        return self.commit_preview(self.preview(drafts, allow_pending=allow_pending))

    def select_leaf(self, leaf_id: str) -> SessionProjection:
        snapshot = self.snapshot()
        candidate = apply_batch(snapshot, (), leaf_id=leaf_id, expected_revision=snapshot.revision)
        projection = self._project(candidate, allow_pending=False)
        self._store.commit((), leaf_id=leaf_id, expected_revision=snapshot.revision)
        return projection

    def recover_pending_tools(self) -> tuple[str, ...]:
        pending = self.projection(allow_pending=True).pending_calls
        if not pending:
            return ()
        self.commit(
            [
                MessageDraft(
                    ToolResultMessage(
                        call.id,
                        call.name,
                        [
                            TextContent(
                                "Result unknown: execution was interrupted; it was not replayed."
                            )
                        ],
                        True,
                        now_ms(),
                        details={"status": "unknown", "recovered": True},
                    )
                )
                for call in pending
            ]
        )
        return tuple(call.id for call in pending)

    def close(self) -> None:
        self._store.close()
