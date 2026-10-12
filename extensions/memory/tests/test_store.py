from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from moonfox_app.extension_api import ExtensionConflictError, ExtensionRecordNotFoundError
from moonfox_ext_memory import MemoryScope, ScopedMemoryStore
from moonfox_ext_memory.store import MemoryCandidate


def _save_snapshot(store: ScopedMemoryStore, record_id: str, revision: int, label: str) -> None:
    assert store.save_snapshot(
        "same-snapshot-id",
        f"message-{label}",
        1,
        store.scope_revision(),
        ((record_id, revision),),
        f"injected-{label}",
    )


def test_three_bound_scopes_isolate_colliding_ids_jobs_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("moonfox_ext_memory.store.uuid4", lambda: SimpleNamespace(hex="collision"))
    path = tmp_path / "shared-test-database.sqlite3"
    agent = ScopedMemoryStore(path, MemoryScope("agent", "shared-scope"))
    qq = ScopedMemoryStore(path, MemoryScope("qq", "shared-scope"))
    other_agent = ScopedMemoryStore(path, MemoryScope("agent", "other-scope"))
    try:
        agent_record = agent.remember("scope collision phrase", subject="conversation")
        qq_record = qq.remember("scope collision phrase", subject="conversation")
        other_record = other_agent.remember("scope collision phrase", subject="conversation")
        assert {agent_record.id, qq_record.id, other_record.id} == {"collision"}
        assert agent.search("collision phrase") == (agent_record,)
        assert qq.search("collision phrase") == (qq_record,)
        assert other_agent.search("collision phrase") == (other_record,)
        assert agent.get(qq_record.id) == agent_record

        updated = agent.update(
            agent_record.id,
            agent_record.revision,
            "agent-only changed phrase",
            subject="conversation",
        )
        assert updated.revision == agent_record.revision + 1
        assert agent.search("agent-only")[0].id == agent_record.id
        assert qq.get(qq_record.id).text == "scope collision phrase"
        assert other_agent.get(other_record.id).text == "scope collision phrase"
        with pytest.raises(ExtensionConflictError):
            agent.update(agent_record.id, agent_record.revision, "stale update")

        _save_snapshot(agent, updated.id, updated.revision, "agent")
        _save_snapshot(qq, qq_record.id, qq_record.revision, "qq")
        _save_snapshot(other_agent, other_record.id, other_record.revision, "other")
        assert agent.snapshots()[0].context_text == "injected-agent"
        assert qq.snapshots()[0].context_text == "injected-qq"
        assert other_agent.snapshots()[0].context_text == "injected-other"

        agent.delete(updated.id, updated.revision)
        assert agent.snapshots() == ()
        with pytest.raises(ExtensionRecordNotFoundError):
            agent.get(updated.id)
        assert qq.get(qq_record.id) == qq_record
        assert other_agent.get(other_record.id) == other_record
        assert qq.snapshots()[0].context_text == "injected-qq"
        assert other_agent.snapshots()[0].context_text == "injected-other"

        agent_job, created_agent = agent.queue_extraction("source-entry", "agent payload", "1")
        qq_job, created_qq = qq.queue_extraction("source-entry", "qq payload", "1")
        other_job, created_other = other_agent.queue_extraction(
            "source-entry", "other payload", "1"
        )
        assert created_agent and created_qq and created_other
        assert {agent_job.id, qq_job.id, other_job.id} == {"collision"}
        duplicate, created_duplicate = qq.queue_extraction(
            "source-entry", "different duplicate payload", "1"
        )
        assert not created_duplicate
        assert duplicate.id == qq_job.id
        assert qq.pending_job_for_entry("source-entry", "1") is not None

        old_agent_revision = agent.scope_revision()
        agent.clear(old_agent_revision)
        assert agent.list_jobs()[0].status == "cancelled"
        assert qq.list_jobs()[0].status == "pending"
        assert other_agent.list_jobs()[0].status == "pending"
        assert qq.pending_job_for_entry("source-entry", "1").payload == "qq payload"  # type: ignore[union-attr]
    finally:
        agent.close()
        qq.close()
        other_agent.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO scope_state VALUES ('global', 'scope', 0, 0)")
    finally:
        connection.close()


def test_literal_search_handles_cjk_english_punctuation_and_fts_syntax(tmp_path: Path) -> None:
    with ScopedMemoryStore(tmp_path / "literal.sqlite3", MemoryScope("agent", "search")) as store:
        chinese = store.remember("明天我们去杭州西湖")
        english = store.remember("favorite programming language is Python")

        assert chinese in store.search("杭州")
        assert chinese in store.search("杭州西湖")
        assert english in store.search("PYTHON")
        assert store.search("!!!") == ()
        assert store.search('"no-such-word" OR *') == ()
        assert store.search('" OR 1=1 --') == ()
        assert store.search("NO MATCH EXISTS") == ()
        assert "domain" not in chinese.to_dict()
        assert "scope_id" not in chinese.to_dict()

        changed = store.update(chinese.id, chinese.revision, "改成上海旅行")
        assert store.search("杭州") == ()
        assert store.search("上海") == (changed,)


def test_forget_cas_epoch_job_recovery_and_candidate_guard(tmp_path: Path) -> None:
    with ScopedMemoryStore(tmp_path / "jobs.sqlite3", MemoryScope("qq", "group")) as store:
        record = store.remember("forget me", subject="conversation")
        pending, created = store.queue_extraction("entry-running", "private input", "1")
        assert created
        work = store.claim_job(pending.id, store.scope_epoch())
        assert work is not None and work.payload == "private input"
        assert store.list_jobs()[0].status == "running"

        assert store.recover_running_jobs() == 1
        assert store.list_jobs()[0].status == "unknown"
        assert store.pending_job_for_entry("entry-running", "1") is None
        assert not store.complete_job(
            pending.id,
            work.capture_epoch,
            (MemoryCandidate("should not return", "fact", "conversation", 0.5),),
        )
        assert store.search("return") == ()

        queued, _ = store.queue_extraction("entry-pending", "payload to erase", "1")
        previous_epoch = store.scope_epoch()
        forgotten = store.forget(record.id, record.revision)
        assert not forgotten.active
        assert forgotten.revision == record.revision + 1
        assert store.search("forget") == ()
        assert store.scope_epoch() == previous_epoch + 1
        assert store.list_jobs()[0].status == "cancelled"
        assert store.pending_job_for_entry("entry-pending", "1") is None
        assert not store.complete_job(
            queued.id,
            previous_epoch,
            (MemoryCandidate("reanimated", "fact", "conversation", 0.5),),
        )
        assert store.search("reanimated") == ()
        assert store.list_records(include_forgotten=True)[0].active is False

        with pytest.raises(ExtensionConflictError):
            store.forget(record.id, record.revision)
        current = store.get(record.id)
        assert store.delete(current.id, current.revision)
        with pytest.raises(ExtensionRecordNotFoundError):
            store.get(record.id)


def test_extraction_merge_is_scoped_idempotent_and_low_confidence(tmp_path: Path) -> None:
    path = tmp_path / "merge.sqlite3"
    first = ScopedMemoryStore(path, MemoryScope("agent", "one"))
    second = ScopedMemoryStore(path, MemoryScope("agent", "two"))
    try:
        first_job, _ = first.queue_extraction("entry-same", "source text", "1")
        second_job, _ = second.queue_extraction("entry-same", "source text", "1")
        first_work = first.claim_job(first_job.id, first.scope_epoch())
        second_work = second.claim_job(second_job.id, second.scope_epoch())
        assert first_work is not None and second_work is not None
        candidate = MemoryCandidate("user likes tea", "preference", "conversation", 1.0)
        assert first.complete_job(first_job.id, first_work.capture_epoch, (candidate,))
        assert second.complete_job(second_job.id, second_work.capture_epoch, (candidate,))
        first_record = first.search("tea")[0]
        second_record = second.search("tea")[0]
        assert first_record.id != second_record.id
        assert first_record.source.origin == "extraction"
        assert first_record.source.entry_id == "entry-same"
        assert first_record.confidence == 0.6
        assert first.list_jobs()[0].status == "done"
        assert first.find_job_for_entry("entry-same", "1") is not None

        queued, _ = first.queue_extraction("entry-malicious", "source", "1")
        claimed = first.claim_job(queued.id, first.scope_epoch())
        assert claimed is not None
        with pytest.raises(ValueError):
            first.complete_job(
                queued.id,
                claimed.capture_epoch,
                (MemoryCandidate("forged", "fact", "operator", 0.9),),
            )
        assert first.search("forged") == ()
        assert first.mark_job_failed(queued.id, claimed.capture_epoch)
    finally:
        first.close()
        second.close()


def test_scope_revision_clear_cas_and_snapshot_retention(tmp_path: Path) -> None:
    with ScopedMemoryStore(tmp_path / "clear.sqlite3", MemoryScope("qq", "one")) as store:
        record = store.remember("clearable fact")
        _save_snapshot(store, record.id, record.revision, "clear")
        revision = store.scope_revision()
        with pytest.raises(ExtensionConflictError):
            store.clear(revision - 1)
        assert store.get(record.id) == record
        new_revision = store.clear(revision)
        assert new_revision == revision + 1
        assert store.snapshots() == ()
        assert store.list_records() == ()


def test_scope_validation_and_closed_store(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MemoryScope("invalid", "scope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryScope("agent", "  ")
    store = ScopedMemoryStore(tmp_path / "closed.sqlite3", MemoryScope("agent", "one"))
    store.close()
    with pytest.raises(RuntimeError):
        store.scope_revision()


def test_scope_handle_is_readonly_and_v1_migration_preserves_records(tmp_path: Path) -> None:
    path = tmp_path / "migrate.sqlite3"
    scope = MemoryScope("agent", "one")
    with ScopedMemoryStore(path, scope) as store:
        record = store.remember("migration survives")
        store.queue_extraction("old-entry", "old payload", "1")
        with pytest.raises(AttributeError):
            store.scope = MemoryScope("qq", "one")  # type: ignore[misc]
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE jobs DROP COLUMN budget_ticket")
        connection.execute("PRAGMA user_version=1")
    with ScopedMemoryStore(path, scope) as store:
        assert store.get(record.id).text == "migration survives"
        pending = store.pending_job_for_entry("old-entry", "1")
        assert pending is not None and pending.budget_ticket is None
        store.queue_extraction("new-entry", "new payload", "1", budget_ticket="original-budget")
    with ScopedMemoryStore(path, scope) as store:
        pending = store.pending_job_for_entry("new-entry", "1")
        assert pending is not None and pending.budget_ticket == "original-budget"
        assert "budget_ticket" not in store.list_jobs()[0].to_dict()


@pytest.mark.parametrize("clock_delta", [0, -1000])
def test_snapshot_retention_keeps_just_written_snapshot(tmp_path: Path, monkeypatch, clock_delta):
    clock = [2000]
    monkeypatch.setattr(ScopedMemoryStore, "snapshot_limit", 2)
    monkeypatch.setattr(ScopedMemoryStore, "_now", staticmethod(lambda: clock[0]))
    with ScopedMemoryStore(tmp_path / "snapshots.sqlite3", MemoryScope("agent", "one")) as store:
        record = store.remember("reference")
        for identity in ("z", "y"):
            assert store.save_snapshot(
                identity,
                identity,
                1,
                store.scope_revision(),
                ((record.id, record.revision),),
                "reference",
            )
        clock[0] += clock_delta
        assert store.save_snapshot(
            "a",
            "new",
            2,
            store.scope_revision(),
            ((record.id, record.revision),),
            "newly injected reference",
        )
        snapshots = store.snapshots()
        assert len(snapshots) == 2
        assert any(
            item.id == "a" and item.context_text == "newly injected reference" for item in snapshots
        )
