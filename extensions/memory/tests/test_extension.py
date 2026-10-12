from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from moonfox_ai.api import Model, UserMessage
from moonfox_app.extension_api import (
    AgentTool,
    ContextBlock,
    ContextProvider,
    EventHandler,
    ExtensionConflictError,
    ExtensionRecordNotFoundError,
    ExtensionResources,
    RunOutcome,
    SessionBinding,
    SessionEvent,
    SessionExtensionContext,
    SessionView,
    TaskInfo,
    estimate_text_tokens,
)
from moonfox_ext_memory import (
    EXTENSION,
    MemoryExtension,
    MemoryScope,
    MemorySettings,
    ScopedMemoryStore,
)
from moonfox_ext_memory.extraction import EXTRACTOR_VERSION


class _Handle:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release

    def dispose(self) -> None:
        self._release()


class _FakeAPI:
    def __init__(self) -> None:
        self.events: dict[str, EventHandler] = {}
        self.context_providers: list[ContextProvider] = []
        self.tools: dict[str, AgentTool] = {}

    def on(self, event: str, handler: EventHandler) -> _Handle:
        self.events[event] = handler

        def release() -> None:
            self.events.pop(event, None)

        return _Handle(release)

    def register_context_provider(self, provider: ContextProvider) -> _Handle:
        self.context_providers.append(provider)

        def release() -> None:
            self.context_providers.remove(provider)

        return _Handle(release)

    def register_tool(self, definition: AgentTool) -> _Handle:
        self.tools[definition.name] = definition

        def release() -> None:
            self.tools.pop(definition.name, None)

        return _Handle(release)


class _FakeActions:
    def __init__(self, view: SessionView) -> None:
        self.current = view

    def view(self) -> SessionView:
        return self.current


class _FakeTaskHandle:
    def __init__(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._id = task_id
        self.task = task

    @property
    def id(self) -> str:
        return self._id

    def cancel(self) -> None:
        self.task.cancel()

    async def wait(self) -> None:
        await asyncio.shield(self.task)


class _FakeTasks:
    def __init__(self) -> None:
        self.handles: list[_FakeTaskHandle] = []
        self.names: list[str] = []

    def submit(
        self,
        name: str,
        work: Callable[[], Awaitable[None]],
        *,
        timeout: float | None = None,
    ) -> _FakeTaskHandle:
        async def run() -> None:
            if timeout is None:
                await work()
            else:
                async with asyncio.timeout(timeout):
                    await work()

        task_id = f"fake-task-{len(self.handles) + 1}"
        task = asyncio.create_task(run(), name=name)
        handle = _FakeTaskHandle(task_id, task)
        self.handles.append(handle)
        self.names.append(name)
        return handle

    def list(self) -> tuple[TaskInfo, ...]:
        return tuple(
            TaskInfo(handle.id, name, handle.task.done(), handle.task.cancelled())
            for handle, name in zip(self.handles, self.names, strict=True)
        )

    async def cancel_all(self) -> None:
        current = asyncio.current_task()
        tasks = [
            handle.task
            for handle in self.handles
            if not handle.task.done() and handle.task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_all(self) -> None:
        tasks = [handle.task for handle in self.handles]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class _FakeModels:
    def __init__(
        self,
        tickets: list[str | None],
        response: str,
        *,
        gate: asyncio.Event | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.tickets = tickets.copy()
        self.response = response
        self.gate = gate
        self.failure = failure
        self.ticket_calls = 0
        self.complete_calls: list[dict[str, object]] = []
        self.started = asyncio.Event()

    def current_ticket(self) -> str | None:
        self.ticket_calls += 1
        return self.tickets.pop(0) if self.tickets else None

    async def complete(self, ticket: str, *, system: str, text: str, max_tokens: int) -> str:
        self.complete_calls.append(
            {"ticket": ticket, "system": system, "text": text, "max_tokens": max_tokens}
        )
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return self.response


def _view(
    binding: SessionBinding,
    text: str,
    entry_id: str,
    *,
    revision: int = 1,
    history: tuple[tuple[str, str], ...] = (),
) -> SessionView:
    messages = [
        UserMessage(content=old_text, timestamp=index)
        for index, old_text in enumerate((message_text for _, message_text in history), start=1)
    ]
    messages.append(UserMessage(content=text, timestamp=revision + len(messages)))
    ids = tuple(message_id for message_id, _ in history) + (entry_id,)
    return SessionView(
        binding=binding,
        revision=revision,
        leaf_id=f"leaf-{revision}",
        messages=tuple(messages),
        model=Model(id="test", provider="test", api="test"),
        thinking_level="off",
        message_ids=ids,
    )


def _resources(
    data_dir: Path,
    binding: SessionBinding,
    *,
    domain: str = "agent",
    models: _FakeModels | None = None,
) -> ExtensionResources:
    return ExtensionResources(binding, domain, data_dir, models)  # type: ignore[arg-type]


def _context(
    binding: SessionBinding, view: SessionView, tasks: _FakeTasks
) -> SessionExtensionContext:
    return SessionExtensionContext(binding, _FakeActions(view), tasks)  # type: ignore[arg-type]


def _tool_call(
    tool: AgentTool,
    arguments: dict[str, object],
) -> Awaitable[Any]:
    assert tool.execute is not None
    return tool.execute("tool-call", arguments, None, None)


async def _emit_settled(api: _FakeAPI, binding: SessionBinding, revision: int = 1) -> None:
    handler = cast(Callable[[SessionEvent], Awaitable[None]], api.events["agent_settled"])
    await handler(
        SessionEvent(
            type="agent_settled",
            binding=binding,
            revision=revision,
            payload=RunOutcome("completed", 1),
        )
    )


def _read_scope(resources: ExtensionResources) -> ScopedMemoryStore:
    return ScopedMemoryStore(
        resources.data_dir / "memory.sqlite3",
        MemoryScope(resources.domain, resources.binding.scope_id),
    )


@pytest.mark.asyncio
async def test_definition_management_cas_and_strict_arguments(tmp_path: Path) -> None:
    binding = SessionBinding("session-managed", "scope-managed")
    models = _FakeModels([], "unused")
    resources = _resources(tmp_path / "managed", binding, models=models)
    assert EXTENSION.manifest.id == "memory"
    assert EXTENSION.manifest.scope == "session"
    assert EXTENSION.tool_names == (
        "memory.search",
        "memory.get",
        "memory.remember",
        "memory.forget",
    )
    assert EXTENSION.commands == (
        "list",
        "get",
        "remember",
        "update",
        "forget",
        "delete",
        "clear",
        "jobs",
    )
    default = MemorySettings.model_validate_json(EXTENSION.default_config_json)
    assert default.recall and default.extraction == "off"
    assert json.loads(EXTENSION.validate_config('{"version":1,"recall":false}')) == {
        "version": 1,
        "recall": False,
        "max_results": 6,
        "max_reference_tokens": 800,
        "extraction": "off",
        "extraction_max_tokens": 512,
    }
    with pytest.raises(ValidationError):
        EXTENSION.validate_config('{"version":1,"unexpected":true}')
    with pytest.raises(ValidationError):
        MemorySettings.model_validate_json('{"version":1,"recall":1}')

    assert EXTENSION.manage is not None
    remember = json.loads(await EXTENSION.manage(resources, "remember", '{"text":"manager note"}'))
    record = remember["record"]
    assert record["source"] == {"origin": "manual", "entry_id": None}
    assert "domain" not in record and "scope_id" not in record
    duplicate = json.loads(
        await EXTENSION.manage(resources, "remember", '{"text":"manager note"}')
    )["record"]
    assert duplicate["id"] == record["id"]
    assert duplicate["revision"] == record["revision"]

    listed = json.loads(await EXTENSION.manage(resources, "list", "{}"))
    assert listed["revision"] >= 1 and listed["records"][0]["id"] == record["id"]
    fetched = json.loads(await EXTENSION.manage(resources, "get", json.dumps({"id": record["id"]})))
    assert fetched["record"]["text"] == "manager note"
    updated = json.loads(
        await EXTENSION.manage(
            resources,
            "update",
            json.dumps(
                {
                    "id": record["id"],
                    "expected_revision": record["revision"],
                    "text": "changed note",
                }
            ),
        )
    )["record"]
    with pytest.raises(ExtensionConflictError):
        await EXTENSION.manage(
            resources,
            "update",
            json.dumps(
                {
                    "id": record["id"],
                    "expected_revision": record["revision"],
                    "text": "stale note",
                }
            ),
        )
    forgotten = json.loads(
        await EXTENSION.manage(
            resources,
            "forget",
            json.dumps({"id": record["id"], "expected_revision": updated["revision"]}),
        )
    )["record"]
    assert not forgotten["active"]
    assert json.loads(await EXTENSION.manage(resources, "list", "{}"))["records"] == []
    assert (
        len(
            json.loads(await EXTENSION.manage(resources, "list", '{"include_forgotten":true}'))[
                "records"
            ]
        )
        == 1
    )
    with pytest.raises(ValidationError):
        await EXTENSION.manage(resources, "list", '{"scope_id":"other"}')
    jobs = json.loads(await EXTENSION.manage(resources, "jobs", "{}"))["jobs"]
    assert jobs == []
    assert models.ticket_calls == 0 and models.complete_calls == []

    deletion = json.loads(
        await EXTENSION.manage(
            resources,
            "delete",
            json.dumps({"id": record["id"], "expected_revision": forgotten["revision"]}),
        )
    )
    assert deletion["deleted"] is True
    assert "Original chat history" in deletion["notice"]
    with pytest.raises(ExtensionRecordNotFoundError):
        await EXTENSION.manage(resources, "get", json.dumps({"id": record["id"]}))

    current = json.loads(await EXTENSION.manage(resources, "list", "{}"))["revision"]
    cleared = json.loads(
        await EXTENSION.manage(resources, "clear", json.dumps({"expected_revision": current}))
    )
    assert cleared["deleted"] is True


@pytest.mark.asyncio
async def test_attach_binding_context_cache_tool_source_and_detach(tmp_path: Path) -> None:
    binding = SessionBinding("session-ctx", "scope-ctx")
    resources = _resources(tmp_path / "context", binding)
    assert EXTENSION.manage is not None
    record = json.loads(await EXTENSION.manage(resources, "remember", '{"text":"喜欢苹果水果"}'))[
        "record"
    ]

    first_view = _view(binding, "我喜欢苹果", "entry-first")
    actions = _FakeActions(first_view)
    tasks = _FakeTasks()
    ctx = SessionExtensionContext(binding, actions, tasks)  # type: ignore[arg-type]
    api = _FakeAPI()
    extension = EXTENSION.factory(resources, EXTENSION.default_config_json)
    assert isinstance(extension, MemoryExtension)
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(ctx)

    provider = api.context_providers[0]
    first = await provider(first_view)
    assert len(first) == 1 and isinstance(first[0], ContextBlock)
    assert first[0].source == "memory"
    assert "Unverified memory references" in first[0].text
    assert "喜欢苹果水果" in first[0].text
    assert estimate_text_tokens(first[0].text) <= extension.settings.max_reference_tokens
    assert await provider(first_view) == first
    with _read_scope(resources) as store:
        snapshots = store.snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].message_id == "entry-first"
        assert [(item.id, item.revision) for item in snapshots[0].records] == [
            (record["id"], record["revision"])
        ]

    updated = json.loads(
        await EXTENSION.manage(
            resources,
            "update",
            json.dumps(
                {
                    "id": record["id"],
                    "expected_revision": record["revision"],
                    "text": "喜欢苹果和梨",
                }
            ),
        )
    )["record"]
    refreshed = await provider(first_view)
    assert refreshed != first and "喜欢苹果和梨" in refreshed[0].text
    assert "喜欢苹果水果" not in refreshed[0].text
    with _read_scope(resources) as store:
        assert len(store.snapshots()) == 1
        assert store.snapshots()[0].records[0].revision == updated["revision"]

    second_view = _view(binding, "我的猫叫小白", "entry-tool-continuation", revision=2)
    actions.current = second_view
    remember = await _tool_call(
        api.tools["memory.remember"],
        {"text": "猫叫小白", "kind": "fact"},
    )
    assert not remember.is_error
    assert remember.structured_content is not None
    remembered = remember.structured_content["record"]
    assert remembered["source"] == {
        "origin": "tool",
        "entry_id": "entry-tool-continuation",
    }
    search = await _tool_call(api.tools["memory.search"], {"query": "小白", "limit": 6})
    assert not search.is_error
    assert search.structured_content["records"][0]["id"] == remembered["id"]
    invalid = await _tool_call(
        api.tools["memory.search"],
        {"query": "小白", "domain": "qq"},
    )
    assert invalid.is_error
    recalled = await provider(second_view)
    assert recalled and "猫叫小白" in recalled[0].text

    forgotten = await _tool_call(
        api.tools["memory.forget"],
        {"id": remembered["id"], "expected_revision": remembered["revision"]},
    )
    assert not forgotten.is_error
    assert not forgotten.structured_content["record"]["active"]
    denied = await _tool_call(api.tools["memory.get"], {"id": remembered["id"]})
    assert denied.is_error and "猫叫小白" not in str(denied.content)
    assert "猫叫小白" not in str(forgotten.content)
    managed = json.loads(
        await EXTENSION.manage(resources, "get", json.dumps({"id": remembered["id"]}))
    )
    assert managed["record"]["text"] == "猫叫小白" and not managed["record"]["active"]
    assert await provider(second_view) == ()

    old_tool = api.tools["memory.get"]
    active_store = extension._store
    assert active_store is not None
    await extension.detach()
    assert active_store.closed
    assert extension._store is None and len(extension._cache) == 0
    assert await provider(second_view) == ()
    stale_tool_result = await _tool_call(old_tool, {"id": record["id"]})
    assert stale_tool_result.is_error
    await extension.dispose()
    assert not api.tools


@pytest.mark.asyncio
async def test_mismatched_attach_rejected_before_opening_scope(tmp_path: Path) -> None:
    binding = SessionBinding("session-valid", "scope-valid")
    resources = _resources(tmp_path / "not-created", binding)
    api = _FakeAPI()
    extension = EXTENSION.factory(resources, EXTENSION.default_config_json)
    await extension.setup(api)  # type: ignore[arg-type]
    other_binding = SessionBinding("session-other", "scope-valid")
    wrong_context = _context(other_binding, _view(other_binding, "hi", "entry"), _FakeTasks())
    with pytest.raises(ValueError, match="does not match"):
        await extension.attach(wrong_context)
    assert not resources.data_dir.exists()
    assert await api.context_providers[0](_view(binding, "hi", "entry")) == ()
    await extension.dispose()


@pytest.mark.asyncio
async def test_store_creation_failure_closes_resources(tmp_path: Path) -> None:
    binding = SessionBinding("session-broken-db", "scope-broken-db")
    data_dir = tmp_path / "broken"
    data_dir.mkdir()
    database = data_dir / "memory.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    models = _FakeModels(["unused-ticket"], "unused")
    resources = _resources(data_dir, binding, models=models)
    api = _FakeAPI()
    tasks = _FakeTasks()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(api)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="newer"):
        await extension.attach(_context(binding, _view(binding, "text", "entry"), tasks))
    assert extension._store is None
    assert tasks.handles == []
    assert models.ticket_calls == 0 and models.complete_calls == []
    with sqlite3.connect(database) as verify:
        assert verify.execute("PRAGMA user_version").fetchone()[0] == 99
    await extension.dispose()


@pytest.mark.asyncio
async def test_settled_extraction_is_owned_nonblocking_and_sends_only_latest_user(
    tmp_path: Path,
) -> None:
    binding = SessionBinding("session-extract", "scope-extract")
    gate = asyncio.Event()
    response = json.dumps(
        {
            "candidates": [
                {
                    "text": "prefers quiet parks",
                    "kind": "preference",
                    "subject": "conversation",
                    "confidence": 0.95,
                }
            ]
        }
    )
    models = _FakeModels(["ticket-for-source"], response, gate=gate)
    resources = _resources(tmp_path / "extract", binding, models=models)
    view = _view(
        binding,
        "latest only user sentence",
        "entry-latest",
        history=(("entry-old", "older private user history"),),
    )
    tasks = _FakeTasks()
    api = _FakeAPI()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(_context(binding, view, tasks))

    await _emit_settled(api, binding, view.revision)
    await asyncio.wait_for(models.started.wait(), timeout=2.0)
    assert len(models.complete_calls) == 1
    call = models.complete_calls[0]
    assert call["ticket"] == "ticket-for-source"
    assert call["text"] == "latest only user sentence"
    assert "older private user history" not in str(call["text"])
    assert call["max_tokens"] == extension.settings.extraction_max_tokens
    assert not tasks.handles[0].task.done()

    gate.set()
    await tasks.wait_all()
    with _read_scope(resources) as store:
        jobs = store.list_jobs()
        assert len(jobs) == 1 and jobs[0].status == "done"
        records = store.search("parks")
        assert len(records) == 1
        assert records[0].source.origin == "extraction"
        assert records[0].source.entry_id == "entry-latest"
        assert records[0].subject == "conversation"
        assert records[0].confidence <= 0.6
    jobs_json = json.loads(await EXTENSION.manage(resources, "jobs", "{}"))["jobs"]
    assert "payload" not in jobs_json[0] and "ticket" not in jobs_json[0]
    await _emit_settled(api, binding, view.revision)
    assert models.ticket_calls == 1 and len(models.complete_calls) == 1
    await extension.detach()
    await extension.dispose()


@pytest.mark.asyncio
async def test_missing_budget_ticket_and_unknown_completion_are_never_retried(
    tmp_path: Path,
) -> None:
    binding = SessionBinding("session-budget", "scope-budget")
    no_ticket = _FakeModels([None], "unused")
    resources = _resources(tmp_path / "no-ticket", binding, models=no_ticket)
    view = _view(binding, "budget unavailable", "entry-no-ticket")
    api = _FakeAPI()
    tasks = _FakeTasks()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(_context(binding, view, tasks))
    await _emit_settled(api, binding)
    await _emit_settled(api, binding)
    assert no_ticket.ticket_calls == 1 and no_ticket.complete_calls == []
    with _read_scope(resources) as store:
        assert store.list_jobs() == ()
    await extension.detach()
    await extension.dispose()

    failure_binding = SessionBinding("session-unknown", "scope-unknown")
    failed_models = _FakeModels(
        ["one-shot-ticket"],
        "unused",
        failure=RuntimeError("private model error must not be persisted"),
    )
    failed_resources = _resources(tmp_path / "unknown", failure_binding, models=failed_models)
    failed_view = _view(failure_binding, "sensitive user input", "entry-unknown")
    failed_tasks = _FakeTasks()
    failed_api = _FakeAPI()
    failed_extension = EXTENSION.factory(
        failed_resources, MemorySettings(extraction="model").model_dump_json()
    )
    await failed_extension.setup(failed_api)  # type: ignore[arg-type]
    await failed_extension.attach(_context(failure_binding, failed_view, failed_tasks))
    await _emit_settled(failed_api, failure_binding)
    await failed_tasks.wait_all()
    await _emit_settled(failed_api, failure_binding)
    assert failed_models.ticket_calls == 1 and len(failed_models.complete_calls) == 1
    with _read_scope(failed_resources) as store:
        assert store.list_jobs()[0].status == "unknown"
        assert store.search("sensitive") == ()
    jobs_json = json.loads(await EXTENSION.manage(failed_resources, "jobs", "{}"))["jobs"]
    assert "sensitive user input" not in json.dumps(jobs_json)
    assert "private model error" not in json.dumps(jobs_json)
    await failed_extension.detach()
    await failed_extension.dispose()


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        json.dumps(
            {
                "candidates": [
                    {
                        "text": "forged source",
                        "kind": "fact",
                        "subject": "conversation",
                        "confidence": 0.9,
                        "source": {"origin": "manual", "entry_id": "forged"},
                    }
                ]
            }
        ),
        json.dumps(
            {
                "candidates": [
                    {
                        "text": "operator claim",
                        "kind": "fact",
                        "subject": "operator",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_malicious_candidate_never_reaches_records(
    tmp_path: Path, response: str
) -> None:
    binding = SessionBinding("session-invalid-model", "scope-invalid-model")
    models = _FakeModels(["ticket"], response)
    resources = _resources(tmp_path / "invalid", binding, models=models)
    view = _view(binding, "member Alice says she likes cats", "entry-invalid")
    tasks = _FakeTasks()
    api = _FakeAPI()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(_context(binding, view, tasks))
    await _emit_settled(api, binding)
    await tasks.wait_all()
    with _read_scope(resources) as store:
        assert store.list_jobs()[0].status == "failed"
        assert store.list_records() == ()
    await extension.detach()
    await extension.dispose()


@pytest.mark.asyncio
async def test_delete_during_blocked_worker_prevents_candidate_resurrection(tmp_path: Path) -> None:
    binding = SessionBinding("session-delete-race", "scope-delete-race")
    gate = asyncio.Event()
    output = json.dumps(
        {
            "candidates": [
                {
                    "text": "must not be resurrected",
                    "kind": "fact",
                    "subject": "conversation",
                    "confidence": 0.8,
                }
            ]
        }
    )
    models = _FakeModels(["blocking-ticket"], output, gate=gate)
    resources = _resources(tmp_path / "delete-race", binding, models=models)
    assert EXTENSION.manage is not None
    record = json.loads(
        await EXTENSION.manage(resources, "remember", '{"text":"delete this first"}')
    )["record"]
    view = _view(binding, "new unrelated user text", "entry-blocked")
    tasks = _FakeTasks()
    api = _FakeAPI()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(_context(binding, view, tasks))
    await _emit_settled(api, binding)
    await asyncio.wait_for(models.started.wait(), timeout=2.0)

    deleted = json.loads(
        await EXTENSION.manage(
            resources,
            "delete",
            json.dumps({"id": record["id"], "expected_revision": record["revision"]}),
        )
    )
    assert deleted["deleted"] is True
    gate.set()
    await tasks.wait_all()
    with _read_scope(resources) as store:
        assert store.search("resurrected") == ()
        assert store.list_records() == ()
        assert store.list_jobs()[0].status == "cancelled"
    await extension.detach()
    await extension.dispose()


@pytest.mark.asyncio
async def test_pending_recovery_and_running_becomes_unknown(tmp_path: Path) -> None:
    pending_binding = SessionBinding("session-pending", "scope-pending")
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    pending_resources = _resources(pending_dir, pending_binding)
    with _read_scope(pending_resources) as store:
        job, created = store.queue_extraction(
            "entry-pending", "recover this text", EXTRACTOR_VERSION, budget_ticket="original-ticket"
        )
        assert created and job.status == "pending"

    pending_models = _FakeModels(
        ["must-not-borrow-new-ticket"],
        json.dumps(
            {
                "candidates": [
                    {
                        "text": "resumed fact",
                        "kind": "fact",
                        "subject": "conversation",
                        "confidence": 0.7,
                    }
                ]
            }
        ),
    )
    pending_resources = _resources(pending_dir, pending_binding, models=pending_models)
    pending_view = _view(pending_binding, "a newer unrelated input", "entry-newer")
    pending_tasks = _FakeTasks()
    pending_api = _FakeAPI()
    pending_extension = EXTENSION.factory(
        pending_resources, MemorySettings(extraction="model").model_dump_json()
    )
    await pending_extension.setup(pending_api)  # type: ignore[arg-type]
    await pending_extension.attach(_context(pending_binding, pending_view, pending_tasks))
    await pending_tasks.wait_all()
    assert pending_models.ticket_calls == 0 and len(pending_models.complete_calls) == 1
    assert pending_models.complete_calls[0]["ticket"] == "original-ticket"
    assert pending_models.complete_calls[0]["text"] == "recover this text"
    with _read_scope(pending_resources) as store:
        assert store.list_jobs()[0].status == "done"
        assert store.search("resumed")[0].source.entry_id == "entry-pending"
    await pending_extension.detach()
    await pending_extension.dispose()

    running_binding = SessionBinding("session-running", "scope-running")
    running_dir = tmp_path / "running"
    running_dir.mkdir()
    running_resources = _resources(running_dir, running_binding)
    with _read_scope(running_resources) as store:
        running, _ = store.queue_extraction("entry-abandoned", "private running payload", "1")
        claimed = store.claim_job(running.id, store.scope_epoch())
        assert claimed is not None

    running_models = _FakeModels(["should-not-be-used"], "unused")
    running_resources = _resources(running_dir, running_binding, models=running_models)
    running_view = _view(running_binding, "different latest input", "entry-other")
    running_api = _FakeAPI()
    running_tasks = _FakeTasks()
    running_extension = EXTENSION.factory(
        running_resources, MemorySettings(extraction="model").model_dump_json()
    )
    await running_extension.setup(running_api)  # type: ignore[arg-type]
    await running_extension.attach(_context(running_binding, running_view, running_tasks))
    assert running_models.ticket_calls == 0 and running_models.complete_calls == []
    with _read_scope(running_resources) as store:
        assert store.find_job_for_entry("entry-abandoned", "1").status == "unknown"  # type: ignore[union-attr]
        assert store.pending_job_for_entry("entry-abandoned", "1") is None
    await running_extension.detach()
    await running_extension.dispose()


@pytest.mark.asyncio
async def test_extraction_off_leaves_pending_without_claiming_a_ticket(tmp_path: Path) -> None:
    binding = SessionBinding("session-off", "scope-off")
    resources = _resources(tmp_path / "off", binding)
    with _read_scope(resources) as store:
        job, _ = store.queue_extraction("entry-off", "never sent while off", "1")
    models = _FakeModels(["must-not-claim"], "unused")
    resources = _resources(tmp_path / "off", binding, models=models)
    api = _FakeAPI()
    tasks = _FakeTasks()
    extension = EXTENSION.factory(resources, EXTENSION.default_config_json)
    await extension.setup(api)  # type: ignore[arg-type]
    await extension.attach(
        _context(binding, _view(binding, "never sent while off", "entry-off"), tasks)
    )
    await _emit_settled(api, binding)
    assert models.ticket_calls == 0 and models.complete_calls == []
    with _read_scope(resources) as store:
        assert store.find_job_for_entry("entry-off", "1").status == "pending"  # type: ignore[union-attr]
        assert store.list_jobs()[0].id == job.id
    await extension.detach()
    await extension.dispose()


async def test_legacy_pending_never_borrows_new_input_budget(tmp_path: Path) -> None:
    binding = SessionBinding("legacy", "legacy-scope")
    resources = _resources(tmp_path, binding)
    with _read_scope(resources) as store:
        store.queue_extraction("entry-old", "old text", EXTRACTOR_VERSION)
    models = _FakeModels(["new-input-budget"], '{"candidates":[]}')
    resources = _resources(tmp_path, binding, models=models)
    tasks = _FakeTasks()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    api = _FakeAPI()
    await extension.setup(api)  # type: ignore[arg-type]
    try:
        await extension.attach(_context(binding, _view(binding, "old text", "entry-old"), tasks))
        await tasks.wait_all()
        assert models.ticket_calls == 0
        assert models.complete_calls == []
        with _read_scope(resources) as store:
            assert store.list_jobs()[0].status == "failed"
    finally:
        await extension.detach()
        await extension.dispose()


@pytest.mark.parametrize("close_while_running", [False, True])
async def test_recovery_queue_is_bounded_and_detach_does_not_start_more_jobs(
    tmp_path: Path, close_while_running: bool
) -> None:
    binding = SessionBinding("bounded", "bounded-scope")
    gate = asyncio.Event()
    models = _FakeModels(["must-not-use"], '{"candidates":[]}', gate=gate)
    resources = _resources(tmp_path, binding, models=models)
    with _read_scope(resources) as store:
        for index in range(6):
            store.queue_extraction(
                f"entry-{index}",
                f"source {index}",
                EXTRACTOR_VERSION,
                budget_ticket=f"budget-{index}",
            )
    tasks = _FakeTasks()
    extension = EXTENSION.factory(resources, MemorySettings(extraction="model").model_dump_json())
    await extension.setup(_FakeAPI())  # type: ignore[arg-type]
    await extension.attach(_context(binding, _view(binding, "newest", "newest"), tasks))
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if len(models.complete_calls) == 4:
                break
        assert len(models.complete_calls) == 4
        assert len(tasks.handles) == 4 and models.ticket_calls == 0
        if close_while_running:
            await extension.detach()
            assert len(tasks.handles) == 4
            assert all(handle.task.done() for handle in tasks.handles)
            with _read_scope(resources) as store:
                statuses = [job.status for job in store.list_jobs()]
                assert statuses.count("pending") == 2 and statuses.count("unknown") == 4
        else:
            gate.set()
            for _ in range(100):
                await asyncio.sleep(0)
                with _read_scope(resources) as store:
                    if all(job.status == "done" for job in store.list_jobs()):
                        break
            else:
                pytest.fail("Bounded recovery queue did not drain")
            assert len(models.complete_calls) == 6 and models.ticket_calls == 0
            assert {call["ticket"] for call in models.complete_calls} == {
                f"budget-{index}" for index in range(6)
            }
    finally:
        gate.set()
        await extension.detach()
        await extension.dispose()
