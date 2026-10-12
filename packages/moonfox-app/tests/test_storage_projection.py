import hashlib
from dataclasses import asdict, replace

import pytest

from moonfox_ai.api import (
    AssistantMessage,
    ImageContent,
    Model,
    NestedToolCall,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    Usage,
    UserMessage,
    get_current_system_prompt,
    get_current_tools,
)
from moonfox_app.extension_api import CustomEntryDraft, SessionBinding
from moonfox_app.runtime.session.entries import (
    CompactionDraft,
    ContextEditDraft,
    MessageDraft,
    MessageEntry,
    ModelChangeDraft,
    ThinkingLevelDraft,
    entry_data,
    parse_entry,
)
from moonfox_app.runtime.session.manager import SessionManager
from moonfox_app.runtime.session.projection import IncompatibleContextError, ProjectionError
from moonfox_app.runtime.storage import (
    InMemorySessionStore,
    JsonlSessionStore,
    StoreBusyError,
    StoreConflictError,
    StoreCorruptionError,
    StoreError,
)
from moonfox_app.runtime.storage.base import json_bytes, validate_entry

MODEL = Model("test", "faux", "faux")
BINDING = SessionBinding("s1", "opaque-scope")


def assistant(*content):
    return AssistantMessage(list(content), "faux", "faux", "test", Usage(), "tool_use", 1)


def manager(store=None, **kwargs):
    return SessionManager.create(
        store or InMemorySessionStore(), binding=BINDING, model=MODEL, **kwargs
    )


@pytest.mark.parametrize(
    "message",
    [
        SystemMessage("base", 1, {"x": "y"}, [ToolDeclaration("tool", "desc", {})]),
        UserMessage("hello", 1),
        UserMessage([TextContent("hi"), ImageContent("data", "image/png")], 2),
        assistant(
            TextContent("hello"),
            ThinkingContent("think"),
            ImageContent("x", "image/png"),
            ToolCall("id", "tool", {"x": [1, True, None]}),
        ),
        ToolResultMessage(
            "id",
            "tool",
            [TextContent("ok")],
            False,
            2,
            details={"d": [1, {"nested": "a"}]},
            structured_content={"x": 1},
            usage=Usage(output=3),
            nested_calls=[
                NestedToolCall(
                    ToolCall("id/1", "sub", {}), ToolResultMessage("id/1", "sub", [], True, 2)
                )
            ],
        ),
    ],
)
def test_codec_roundtrips_every_message_role_and_content(message):
    entry = MessageEntry("e", "h", 1, message)
    recovered = validate_entry(entry)
    assert recovered == entry
    assert type(recovered.message) is type(message)
    assert recovered is not entry


@pytest.mark.parametrize("change", ["role", "content_tag", "unknown_field"])
def test_codec_never_silently_drops_unknown_context(change):
    raw = entry_data(MessageEntry("e", "h", 1, UserMessage([TextContent("hi")], 1)))
    if change == "role":
        raw["message"]["role"] = "future"
    elif change == "content_tag":
        raw["message"]["content"][0]["type"] = "future"
    else:
        raw["message"]["unknown"] = 1
    with pytest.raises(ValueError):
        parse_entry(raw)


@pytest.mark.parametrize("persistent", [False, True])
def test_store_snapshots_and_atomic_validation(tmp_path, persistent):
    store = (
        JsonlSessionStore(tmp_path / "session.jsonl", create=True)
        if persistent
        else InMemorySessionStore()
    )
    mgr = manager(store)
    before = mgr.snapshot()
    with pytest.raises(ValueError, match="orphan"):
        store.commit(
            [MessageEntry("bad", "unknown", 1, UserMessage("bad", 1))],
            leaf_id="bad",
            expected_revision=before.revision,
        )
    assert mgr.snapshot() == before
    with pytest.raises(StoreConflictError):
        store.commit([], leaf_id=before.leaf_id, expected_revision=0)
    mgr.commit([CustomEntryDraft("test.state", {"a": [1]})])
    snapshot = mgr.snapshot()
    snapshot.entries[-1].data["a"].append(2)
    assert mgr.snapshot().entries[-1].data == {"a": [1]}
    store.close()
    with pytest.raises(StoreError):
        store.snapshot()


def test_jsonl_scope_binding_reopen_and_writer_lock(tmp_path):
    path = tmp_path / "session.jsonl"
    store = JsonlSessionStore(path, create=True)
    mgr = manager(store, system_prompt="stable")
    mgr.commit([MessageDraft(UserMessage("hello", 1))])
    expected = mgr.snapshot()
    with pytest.raises(StoreBusyError):
        JsonlSessionStore(path)
    with pytest.raises(FileExistsError):
        JsonlSessionStore(path, create=True)
    store.close()
    with JsonlSessionStore(path) as reopened:
        assert reopened.snapshot() == expected
        with pytest.raises(PermissionError):
            SessionManager(reopened, binding=SessionBinding("s1", "other-scope"))
        restored = SessionManager(reopened, binding=BINDING)
        assert restored.projection().messages[-1] == UserMessage("hello", 1)


@pytest.mark.parametrize("tail", [b'{"record":', b"broken\n", b"\xff"])
def test_torn_tail_recovers_only_last_committed_batch(tmp_path, tail):
    path = tmp_path / "session.jsonl"
    store = JsonlSessionStore(path, create=True)
    mgr = manager(store)
    baseline = mgr.snapshot()
    store.close()
    size = path.stat().st_size
    with path.open("ab") as file:
        file.write(tail)
    with JsonlSessionStore(path) as reopened:
        assert reopened.snapshot() == baseline
        assert reopened.recovered_bytes == len(tail)
        assert path.stat().st_size == size


def test_uncommitted_whole_batch_is_discarded_then_append_works(tmp_path):
    path = tmp_path / "s.jsonl"
    store = JsonlSessionStore(path, create=True)
    mgr = manager(store)
    baseline = mgr.snapshot()
    size = path.stat().st_size
    mgr.commit([CustomEntryDraft("a.one", 1), CustomEntryDraft("a.two", 2)])
    store.close()
    data = path.read_bytes()
    path.write_bytes(b"\n".join(data.splitlines()[:-1]) + b"\n")
    with JsonlSessionStore(path) as reopened:
        assert reopened.snapshot() == baseline
        assert path.stat().st_size == size
        restored = SessionManager(reopened, binding=BINDING)
        restored.commit([MessageDraft(UserMessage("after recovery", 1))])
    with JsonlSessionStore(path) as reopened:
        assert len(reopened.snapshot().entries) == len(baseline.entries) + 1


def test_mid_file_corruption_and_bad_checksum_are_not_truncated(tmp_path):
    path = tmp_path / "s.jsonl"
    store = JsonlSessionStore(path, create=True)
    manager(store).commit([MessageDraft(UserMessage("hello", 1))])
    store.close()
    good = path.read_bytes()
    for corrupted in (b"broken\n" + good, good.replace(b"hello", b"jello")):
        path.write_bytes(corrupted)
        with pytest.raises(StoreCorruptionError):
            JsonlSessionStore(path)
        assert path.read_bytes() == corrupted


def test_even_checksum_valid_orphan_parent_is_corrupt(tmp_path):
    path = tmp_path / "s.jsonl"
    store = JsonlSessionStore(path, create=True)
    mgr = manager(store)
    revision = mgr.snapshot().revision
    store.close()
    item = MessageEntry("bad", "missing", 1, UserMessage("x", 1))
    body = (
        json_bytes(
            {"record": "begin", "tx": "t", "revision": revision + 1, "count": 1, "leaf_id": "bad"}
        )
        + b"\n"
    )
    body += json_bytes({"record": "entry", "tx": "t", "entry": asdict(item)}) + b"\n"
    with path.open("ab") as file:
        file.write(
            body
            + json_bytes(
                {"record": "commit", "tx": "t", "digest": hashlib.sha256(body).hexdigest()}
            )
            + b"\n"
        )
    with pytest.raises(StoreCorruptionError, match="orphan"):
        JsonlSessionStore(path)


def test_failed_fsync_rolls_back_disk_and_in_memory(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    store = JsonlSessionStore(path, create=True)
    mgr = manager(store)
    before = mgr.snapshot()
    size = path.stat().st_size
    sync = store._sync
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk failed")
        sync()

    monkeypatch.setattr(store, "_sync", fail_once)
    with pytest.raises(OSError):
        mgr.commit([MessageDraft(UserMessage("no partial commit", 1))])
    assert mgr.snapshot() == before
    assert path.stat().st_size == size
    store.close()
    with JsonlSessionStore(path) as reopened:
        assert reopened.snapshot() == before


def test_preview_is_detached_stale_and_invalid_batches_never_write():
    mgr = manager()
    before = mgr.snapshot()
    preview = mgr.preview([MessageDraft(UserMessage("preview", 1))])
    assert mgr.snapshot() == before
    mgr.commit([MessageDraft(UserMessage("other", 2))])
    with pytest.raises(StoreConflictError):
        mgr.commit_preview(preview)
    before = mgr.snapshot()
    with pytest.raises(ValueError):
        mgr.commit([CustomEntryDraft("a.good", 1), CustomEntryDraft("unqualified", 2)])
    assert mgr.snapshot() == before
    with pytest.raises(ValueError):
        mgr.commit([CustomEntryDraft("a.bad", float("nan"))])


def test_branch_navigation_edits_and_model_replay_preserve_original_entries():
    mgr = manager(system_prompt="base")
    root = mgr.snapshot().leaf_id
    mgr.commit([MessageDraft(UserMessage("original", 1), "user")])
    first_leaf = mgr.snapshot().leaf_id
    model = replace(MODEL, id="new")
    mgr.commit(
        [
            ContextEditDraft("user", UserMessage("corrected", 1)),
            ModelChangeDraft(model),
            ThinkingLevelDraft("high"),
        ]
    )
    branch_one = mgr.snapshot().leaf_id
    projection = mgr.projection()
    assert projection.messages[-1].content == "corrected"
    assert projection.model == model and projection.thinking_level == "high"
    assert next(e for e in mgr.snapshot().entries if e.id == "user").message.content == "original"
    mgr.select_leaf(root)
    mgr.commit([MessageDraft(UserMessage("sibling", 2))])
    assert mgr.projection().model == MODEL
    with pytest.raises(ProjectionError, match="earlier"):
        mgr.commit([ContextEditDraft("user", UserMessage("cross branch", 1))])
    mgr.select_leaf(branch_one)
    assert mgr.projection().messages[-1].content == "corrected"
    mgr.select_leaf(first_leaf)
    assert mgr.projection().messages[-1].content == "original"


def test_compaction_retains_system_tools_and_complete_pairs():
    mgr = manager(system_prompt="base")
    mgr.commit(
        [
            MessageDraft(SystemMessage("", 1, {"style": "clear"}, [ToolDeclaration("t", "", {})])),
            MessageDraft(UserMessage("old", 1), "old"),
            MessageDraft(assistant(ToolCall("c", "t", {})), "call"),
            MessageDraft(ToolResultMessage("c", "t", [TextContent("ok")], False, 2), "result"),
            MessageDraft(UserMessage("recent", 3), "recent"),
        ]
    )
    before = mgr.snapshot()
    with pytest.raises(ValueError):
        mgr.commit([CompactionDraft("bad split", "result")])
    assert mgr.snapshot() == before
    with pytest.raises(ProjectionError, match="visible"):
        mgr.commit([CompactionDraft("unknown cut", "missing")])
    mgr.commit([CompactionDraft("old facts", "call")])
    projected = mgr.projection()
    assert get_current_system_prompt(projected.messages) == "base\n\nclear"
    assert [tool.name for tool in get_current_tools(projected.messages)] == ["t"]
    assert [m.role for m in projected.messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "user",
    ]
    mgr.commit([CompactionDraft("corrected facts, not cumulative text", "recent")])
    assert "old facts" not in str(mgr.projection().messages)
    assert len(mgr.snapshot().entries) == len(before.entries) + 2
    mgr.commit([CompactionDraft("self-contained", None), MessageDraft(UserMessage("after", 4))])
    assert mgr.projection().messages[-1].content == "after"


def test_unknown_custom_data_survives_without_plugin_but_required_context_fails():
    store = InMemorySessionStore()
    mgr = manager(store)
    mgr.commit([CustomEntryDraft("absent.state", {"opaque": [1, "two"]})])
    assert len(mgr.projection().messages) == 0
    assert mgr.snapshot().entries[-1].data == {"opaque": [1, "two"]}
    with pytest.raises(IncompatibleContextError):
        mgr.commit([CustomEntryDraft("absent.context", "required", True)])
    codec = {"known.context": lambda draft, timestamp: [UserMessage(draft.data, timestamp)]}
    mgr = SessionManager(store, binding=BINDING, codecs=codec)
    mgr.commit([CustomEntryDraft("known.context", "reference", True)])
    assert mgr.projection().messages[-1].content == "reference"
    with pytest.raises(IncompatibleContextError):
        SessionManager(store, binding=BINDING)


def test_missing_tool_results_recover_as_unknown_without_replay():
    mgr = manager()
    mgr.commit(
        [MessageDraft(assistant(ToolCall("a", "t", {}), ToolCall("b", "t", {})))],
        allow_pending=True,
    )
    mgr.commit([MessageDraft(ToolResultMessage("a", "t", [], False, 2))], allow_pending=True)
    assert mgr.recover_pending_tools() == ("b",)
    result = mgr.projection().messages[-1]
    assert result.tool_call_id == "b" and result.is_error
    assert result.details == {"status": "unknown", "recovered": True}
    assert mgr.recover_pending_tools() == ()


def test_incomplete_pair_in_middle_cannot_be_hidden_by_compaction():
    mgr = manager()
    mgr.commit([MessageDraft(assistant(ToolCall("a", "t", {})))], allow_pending=True)
    before = mgr.snapshot()
    with pytest.raises(ValueError):
        mgr.commit([MessageDraft(UserMessage("bad", 2)), CompactionDraft("hide it", None)])
    assert mgr.snapshot() == before
