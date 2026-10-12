"""Deterministic contracts for the independent session extension infrastructure."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel

from moonfox_agent.api import AgentTool, AgentToolResult, ToolExecutionContext
from moonfox_ai.api import Model, SystemMessage, TextContent, ToolCall, UserMessage
from moonfox_app.extension_api import (
    BoundaryPreview,
    BoundaryResult,
    CommandDefinition,
    ContextBlock,
    CustomEntryDraft,
    Delivery,
    ExtensionManifest,
    PermissionDecision,
    PromptSection,
    RegistrationHandle,
    SessionBinding,
    SessionEvent,
    SessionExtensionAPI,
    SessionExtensionContext,
    SessionExtensionPort,
    SessionView,
    ToolPermissionRequest,
)
from moonfox_app.extensions import (
    CapabilityError,
    CommandAuthorizationError,
    CommandNotFoundError,
    DependencyError,
    ExtensionRegistry,
    ExtensionRevokedError,
    LifecycleError,
    ManifestError,
    NamespaceError,
    RegistrationError,
    SessionExtensionManager,
)


class Actions:
    def __init__(self, session_id: str = "one") -> None:
        self.snapshot = SessionView(
            SessionBinding(session_id, "opaque"),
            1,
            "leaf",
            (UserMessage([TextContent("original")], 1),),
            Model("faux", "faux", "faux"),
            "off",
        )
        self.staged: list[CustomEntryDraft] = []
        self.queued: list[tuple[UserMessage, Delivery]] = []

    def view(self) -> SessionView:
        return self.snapshot  # Deliberately NOT detached: the adapter must do that.

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None:
        self.staged.extend(drafts)

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        self.queued.append((message, delivery))


class Plugin:
    def __init__(
        self,
        name: str = "alpha",
        *,
        log: list[str] | None = None,
        setup: Callable[[SessionExtensionAPI], Awaitable[None]] | None = None,
        attach: Callable[[SessionExtensionContext], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.log = [] if log is None else log
        self.setup_hook = setup
        self.attach_hook = attach
        self.api: SessionExtensionAPI
        self.ctx: SessionExtensionContext

    async def setup(self, api: SessionExtensionAPI) -> None:
        self.api = api
        self.log.append(f"{self.name}.setup")
        if self.setup_hook is not None:
            await self.setup_hook(api)

    async def attach(self, ctx: SessionExtensionContext) -> None:
        self.ctx = ctx
        self.log.append(f"{self.name}.attach")
        if self.attach_hook is not None:
            await self.attach_hook(ctx)

    async def detach(self) -> None:
        self.log.append(f"{self.name}.detach")

    async def dispose(self) -> None:
        self.log.append(f"{self.name}.dispose")


def manifest(
    name: str = "alpha",
    capabilities: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    scope: Literal["session", "host"] = "session",
) -> ExtensionManifest:
    return ExtensionManifest(name, "1.2.3", ">=0.1,<0.2", scope, capabilities, requires)


def manager_for(
    plugin: Plugin,
    capabilities: tuple[str, ...] = (),
    *,
    actions: Actions | None = None,
    timeout: float = 0.2,
) -> SessionExtensionManager:
    registry = ExtensionRegistry()
    registry.register(manifest(plugin.name, capabilities), lambda: plugin)
    return SessionExtensionManager(
        registry,
        actions=Actions() if actions is None else actions,
        enabled=(plugin.name,),
        grants={plugin.name: frozenset(capabilities)},
        timeout=timeout,
    )


class Arguments(BaseModel):
    value: int = 1


async def tool_execute(call_id: str, args: Any, context: ToolExecutionContext) -> AgentToolResult:
    return AgentToolResult([TextContent("ok")])


def tool(name: str = "alpha.work") -> AgentTool:
    return AgentTool(name, "test", "Test", Arguments, execute_with_context=tool_execute)


async def nothing() -> None:
    pass


async def forever() -> None:
    await asyncio.Event().wait()


def message_text(view: SessionView) -> str:
    message = view.messages[0]
    assert isinstance(message, UserMessage) and isinstance(message.content, list)
    block = message.content[0]
    assert isinstance(block, TextContent)
    return block.text


def mutate_view(view: SessionView) -> None:
    message = view.messages[0]
    assert isinstance(message, UserMessage) and isinstance(message.content, list)
    block = message.content[0]
    assert isinstance(block, TextContent)
    block.text = "mutated"


@pytest.mark.parametrize("name", ["", "Alpha", "1alpha", "alpha.beta", "alpha/other", "a\n", "a b"])
def test_registry_rejects_invalid_id(name: str) -> None:
    with pytest.raises(ManifestError):
        ExtensionRegistry().register(manifest(name), Plugin)


@pytest.mark.parametrize(
    "change",
    [
        {"version": "not-a-version"},
        {"sdk_version_range": "not-a-range"},
        {"sdk_version_range": ">=5"},
        {"scope": "global"},
        {"capabilities": ("tasks", "tasks")},
        {"requires": ("beta", "beta")},
        {"requires": ("other.extension",)},
        {"capabilities": ["tasks"]},
        {"requires": ["beta"]},
        {"version": 1},
    ],
)
def test_registry_rejects_invalid_manifest(change: dict[str, Any]) -> None:
    with pytest.raises(ManifestError):
        ExtensionRegistry().register(replace(manifest(), **change), Plugin)


@pytest.mark.parametrize(
    "scope,capability",
    [
        ("session", "unknown"),
        ("session", "channel"),
        ("host", "prompt"),
        ("host", "entries"),
        ("host", "events"),
        ("host", "tools"),
    ],
)
def test_registry_rejects_unknown_or_wrong_scope_capability(
    scope: Literal["session", "host"],
    capability: str,
) -> None:
    with pytest.raises(CapabilityError):
        ExtensionRegistry().register(manifest(capabilities=(capability,), scope=scope), Plugin)


def test_registry_duplicate_and_version_support_without_factory_calls() -> None:
    calls: list[str] = []

    def factory() -> Plugin:
        calls.append("factory")
        return Plugin()

    registry = ExtensionRegistry()
    registry.register(replace(manifest("good_name-1"), version="1.0rc1+local"), factory)
    assert registry.resolve((), scope="session") == ()
    with pytest.raises(ManifestError, match="Duplicate"):
        registry.register(manifest("good_name-1"), factory)
    assert [item.manifest.id for item in registry.resolve(("good_name-1",), scope="session")] == [
        "good_name-1"
    ]
    assert calls == []


def test_registry_stable_topological_order_and_explicit_dependencies() -> None:
    registry = ExtensionRegistry()
    registry.register(manifest("alpha", requires=("beta",)), Plugin)
    registry.register(manifest("beta"), Plugin)
    registry.register(manifest("gamma"), Plugin)
    assert [
        entry.manifest.id for entry in registry.resolve(("alpha", "gamma", "beta"), scope="session")
    ] == ["gamma", "beta", "alpha"]
    with pytest.raises(DependencyError, match="explicitly enabled"):
        registry.resolve(("alpha",), scope="session")
    with pytest.raises(DependencyError, match="Duplicate"):
        registry.resolve(("beta", "beta"), scope="session")


@pytest.mark.parametrize("case", ["missing", "disabled", "cycle", "wrong_scope", "unknown"])
async def test_invalid_graph_never_runs_any_factory(case: str) -> None:
    calls: list[str] = []

    def factory() -> Plugin:
        calls.append("factory")
        return Plugin()

    registry = ExtensionRegistry()
    registry.register(manifest("first"), factory)
    registry.register(manifest("alpha", requires=("beta",)), factory)
    names: tuple[str, ...] = ("first", "alpha", "beta")
    if case == "cycle":
        registry.register(manifest("beta", requires=("alpha",)), factory)
    elif case == "wrong_scope":
        registry.register(manifest("beta", scope="host"), factory)
    elif case in ("disabled", "unknown"):
        registry.register(manifest("beta"), factory)
    if case == "disabled":
        names = ("first", "alpha")
    elif case == "unknown":
        names += ("absent",)
    manager = SessionExtensionManager(registry, actions=Actions(), enabled=names)
    with pytest.raises(DependencyError):
        await manager.start()
    assert calls == [] and manager.active_ids == ()


@pytest.mark.parametrize(
    "grant", [None, frozenset(), frozenset({"events"}), frozenset({"tasks", "bogus"})]
)
async def test_grants_fail_before_any_factory(grant: frozenset[str] | None) -> None:
    calls: list[str] = []

    def factory() -> Plugin:
        calls.append("called")
        return Plugin()

    registry = ExtensionRegistry()
    registry.register(manifest("first"), factory)
    registry.register(manifest(capabilities=("tasks",)), factory)
    manager = SessionExtensionManager(
        registry,
        actions=Actions(),
        enabled=("first", "alpha"),
        grants=None if grant is None else {"alpha": grant},
    )
    with pytest.raises(CapabilityError):
        await manager.start()
    assert calls == []


async def test_protocol_empty_manager_is_inert() -> None:
    manager = SessionExtensionManager(ExtensionRegistry(), actions=Actions())
    port: SessionExtensionPort = manager
    await manager.start()
    view = Actions().view()
    assert port.tools() == ()
    assert await port.prompt_sections(view) == ()
    assert await port.context_blocks(view) == ()
    assert (
        await port.check_tool(ToolPermissionRequest(view.binding, "host.tool", "id", {}))
    ).allowed
    initial = BoundaryPreview(view, (CustomEntryDraft("host.initial", {}),))
    assert await port.boundary(
        initial, lambda drafts: replace(initial, drafts=drafts)
    ) == BoundaryResult(initial.drafts)
    await manager.stop()
    await manager.stop()


async def test_capabilities_cannot_be_used_merely_because_host_granted_them() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry,
        actions=Actions(),
        enabled=("alpha",),
        grants={"alpha": frozenset({"entries", "enqueue", "tasks", "events"})},
    )
    await manager.start()
    try:
        with pytest.raises(CapabilityError):
            plugin.api.on("event", lambda event: None)
        with pytest.raises(CapabilityError):
            plugin.ctx.actions.stage_entries((CustomEntryDraft("alpha.data"),))
        with pytest.raises(CapabilityError):
            plugin.ctx.actions.enqueue(UserMessage("hello", 1), "steer")
        with pytest.raises(CapabilityError):
            plugin.ctx.tasks.submit("work", nothing)
        with pytest.raises(CapabilityError):
            _ = plugin.api.tasks
    finally:
        await manager.stop()


async def test_setup_is_registration_only_and_scoped_actions_copy_and_revoke() -> None:
    actions = Actions()
    registered: list[RegistrationHandle] = []

    async def setup(api: SessionExtensionAPI) -> None:
        with pytest.raises(LifecycleError):
            api.enqueue(UserMessage("setup", 1), "follow_up")
        with pytest.raises(LifecycleError):
            api.stage_entries((CustomEntryDraft("alpha.entry"),))
        with pytest.raises(LifecycleError):
            api.tasks.submit("setup", nothing)
        registered.append(api.on("tick", lambda event: None))

    plugin = Plugin(setup=setup)
    manager = manager_for(plugin, ("entries", "enqueue", "tasks", "events"), actions=actions)
    await manager.start()
    assert plugin.ctx.actions is not actions
    view = plugin.ctx.actions.view()
    mutate_view(view)
    assert message_text(actions.view()) == "original"
    data = {"nested": [1]}
    plugin.ctx.actions.stage_entries((CustomEntryDraft("alpha.data", data),))
    data["nested"].append(2)
    assert actions.staged[0].data == {"nested": [1]}
    message = UserMessage([TextContent("safe")], 1)
    plugin.api.enqueue(message, "follow_up")
    assert isinstance(message.content, list) and isinstance(message.content[0], TextContent)
    message.content[0].text = "changed"
    assert actions.queued == [(UserMessage([TextContent("safe")], 1), "follow_up")]
    tasks = plugin.api.tasks
    await manager.stop()
    assert all(item.done for item in tasks.list())
    for operation in (
        lambda: plugin.ctx.actions.view(),
        lambda: plugin.ctx.actions.stage_entries(()),
        lambda: plugin.ctx.actions.enqueue(UserMessage("late", 1), "steer"),
        lambda: plugin.api.on("tick", lambda event: None),
        lambda: tasks.submit("late", nothing),
    ):
        with pytest.raises(ExtensionRevokedError):
            operation()
    registered[0].dispose()
    registered[0].dispose()


@pytest.mark.parametrize(
    "name", ["beta.data", "alphabeta.data", "alpha", "alpha.", "alpha../data", "alpha.a b"]
)
async def test_entry_namespace_checked_through_both_action_handles(name: str) -> None:
    plugin = Plugin()
    actions = Actions()
    manager = manager_for(plugin, ("entries",), actions=actions)
    await manager.start()
    try:
        for submit in (plugin.api.stage_entries, plugin.ctx.actions.stage_entries):
            with pytest.raises(NamespaceError):
                submit((CustomEntryDraft(name),))
        assert actions.staged == []
    finally:
        await manager.stop()


@pytest.mark.parametrize(
    "data", [float("nan"), float("inf"), {1: "not-string-key"}, {"set": {1}}, (1, 2), object()]
)
async def test_entries_require_json_safe_payload(data: Any) -> None:
    plugin = Plugin()
    actions = Actions()
    manager = manager_for(plugin, ("entries",), actions=actions)
    await manager.start()
    try:
        with pytest.raises(RegistrationError):
            plugin.api.stage_entries((CustomEntryDraft("alpha.data", data),))
        assert actions.staged == []
    finally:
        await manager.stop()


async def test_entry_batch_validation_is_atomic_and_rejects_cycles_and_non_custom() -> None:
    plugin = Plugin()
    actions = Actions()
    manager = manager_for(plugin, ("entries",), actions=actions)
    await manager.start()
    try:
        cyclic: list[Any] = []
        cyclic.append(cyclic)
        for bad in (
            CustomEntryDraft("beta.data"),
            CustomEntryDraft("alpha.data", cyclic),
            cast(CustomEntryDraft, UserMessage("spoof", 1)),
        ):
            with pytest.raises(RegistrationError):
                plugin.api.stage_entries((CustomEntryDraft("alpha.good"), bad))
        assert actions.staged == []
    finally:
        await manager.stop()


async def test_enqueue_cannot_spoof_system_tool_or_mutated_user_role() -> None:
    plugin = Plugin()
    actions = Actions()
    manager = manager_for(plugin, ("enqueue",), actions=actions)
    await manager.start()
    spoof = UserMessage("spoof", 1)
    object.__setattr__(spoof, "role", "system")
    try:
        for invalid in (
            SystemMessage("system", 1),
            spoof,
            UserMessage(cast(Any, [ToolCall("id", "x", {})]), 1),
        ):
            with pytest.raises(RegistrationError):
                plugin.ctx.actions.enqueue(cast(UserMessage, invalid), "steer")
        with pytest.raises(RegistrationError):
            plugin.api.enqueue(UserMessage("safe", 1), cast(Delivery, "now"))
        assert actions.queued == []
    finally:
        await manager.stop()


async def test_tools_preserve_identity_and_validate_schema_namespace_and_conflicts() -> None:
    class Service:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise AssertionError("Injected tool service must never be copied")

        async def execute(
            self, call_id: str, args: Any, ctx: ToolExecutionContext
        ) -> AgentToolResult:
            return AgentToolResult([])

    definition = AgentTool(
        "alpha.work", "work", "Work", Arguments, execute_with_context=Service().execute
    )

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_tool(definition)
        with pytest.raises(RegistrationError, match="Duplicate"):
            api.register_tool(definition)
        with pytest.raises(NamespaceError):
            api.register_tool(tool("beta.work"))
        with pytest.raises(RegistrationError):
            api.register_tool(replace(tool("alpha.invalid"), parameters=cast(Any, dict)))

    plugin = Plugin(setup=setup)
    manager = manager_for(plugin, ("tools",))
    await manager.start()
    assert manager.tools()[0] is definition
    await manager.stop()
    assert manager.tools() == ()


async def test_notifications_snapshot_order_detached_payload_and_isolated_timeout() -> None:
    calls: list[tuple[str, list[int]]] = []
    handles: list[RegistrationHandle] = []
    timed_out = asyncio.Event()
    added = False

    async def setup(api: SessionExtensionAPI) -> None:
        def first(event: SessionEvent) -> None:
            nonlocal added
            calls.append(("first", list(event.payload["items"])))
            event.payload["items"].append(9)
            handles[0].dispose()
            if not added:
                api.on("tick", lambda value: calls.append(("late", list(value.payload["items"]))))
                added = True

        def broken(event: SessionEvent) -> None:
            raise RuntimeError("notification failure")

        async def blocked(event: SessionEvent) -> None:
            try:
                await forever()
            finally:
                timed_out.set()

        api.on("tick", first)
        handles.append(
            api.on("tick", lambda event: calls.append(("second", list(event.payload["items"]))))
        )
        api.on("other", lambda event: calls.append(("wrong event", [])))
        api.on("tick", broken)
        api.on("tick", blocked)
        api.on("tick", lambda event: calls.append(("last", list(event.payload["items"]))))

    plugin = Plugin(setup=setup)
    manager = manager_for(plugin, ("events",), timeout=0.01)
    await manager.start()
    event = SessionEvent("tick", plugin.ctx.binding, 1, {"items": [1]})
    try:
        await manager.notify(event)
        assert calls == [("first", [1]), ("second", [1]), ("last", [1])]
        assert event.payload == {"items": [1]} and timed_out.is_set()
        assert len(manager.diagnostics) == 2
        assert "TimeoutError" in manager.diagnostics[-1].message
        calls.clear()
        await manager.notify(event)
        assert calls == [("first", [1]), ("last", [1]), ("late", [1])]
    finally:
        await manager.stop()


async def test_context_providers_drop_failed_or_invalid_contributions_and_detach_views() -> None:
    fail = False
    seen: list[str] = []

    async def setup(api: SessionExtensionAPI) -> None:
        async def mutator(view: SessionView) -> tuple[ContextBlock, ...]:
            seen.append(message_text(view))
            mutate_view(view)
            if fail:
                raise RuntimeError("retrieval offline")
            return (ContextBlock("alpha.retrieval", "fresh"),)

        async def observer(view: SessionView) -> tuple[ContextBlock, ...]:
            seen.append(message_text(view))
            return (ContextBlock("document://reference", "reference"),)

        async def invalid(view: SessionView) -> tuple[ContextBlock, ...]:
            return cast(tuple[ContextBlock, ...], [ContextBlock("alpha.invalid", "must drop")])

        api.register_context_provider(mutator)
        api.register_context_provider(observer)
        api.register_context_provider(invalid)

    actions = Actions()
    manager = manager_for(Plugin(setup=setup), ("context",), actions=actions)
    await manager.start()
    try:
        assert [block.text for block in await manager.context_blocks(actions.view())] == [
            "fresh",
            "reference",
        ]
        fail = True
        assert [block.text for block in await manager.context_blocks(actions.view())] == [
            "reference"
        ]
        assert seen == ["original"] * 4 and message_text(actions.view()) == "original"
        assert len(manager.diagnostics) == 3
    finally:
        await manager.stop()
    assert await manager.context_blocks(actions.view()) == ()


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("kind", ["exception", "namespace", "invalid", "duplicate", "timeout"])
async def test_prompt_required_failures_propagate_optional_failures_are_empty(
    required: bool, kind: str
) -> None:
    async def provider(view: SessionView) -> tuple[PromptSection, ...]:
        if kind == "exception":
            raise ValueError("persona unavailable")
        if kind == "namespace":
            return (PromptSection("beta.persona", "wrong namespace"),)
        if kind == "duplicate":
            return (PromptSection("alpha.persona", "a"), PromptSection("alpha.persona", "b"))
        if kind == "timeout":
            await forever()
        return cast(tuple[PromptSection, ...], ("not a section",))

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_prompt_section(provider, required=required)

    manager = manager_for(Plugin(setup=setup), ("prompt",), timeout=0.01)
    await manager.start()
    try:
        if required:
            with pytest.raises((ValueError, TimeoutError)):
                await manager.prompt_sections(Actions().view())
        else:
            assert await manager.prompt_sections(Actions().view()) == ()
        assert len(manager.diagnostics) == 1
    finally:
        await manager.stop()


async def test_prompt_full_snapshot_conflicts_and_unload_removal() -> None:
    seen: list[str] = []

    async def setup(api: SessionExtensionAPI) -> None:
        async def first(view: SessionView) -> tuple[PromptSection, ...]:
            mutate_view(view)
            return (PromptSection("alpha.persona", "approved"),)

        async def second(view: SessionView) -> tuple[PromptSection, ...]:
            seen.append(message_text(view))
            return (PromptSection("alpha.persona", "conflict"),)

        api.register_prompt_section(first)
        api.register_prompt_section(second, required=False)

    manager = manager_for(Plugin(setup=setup), ("prompt",))
    await manager.start()
    view = Actions().view()
    assert await manager.prompt_sections(view) == (PromptSection("alpha.persona", "approved"),)
    assert seen == ["original"]
    await manager.disable("alpha")
    assert await manager.prompt_sections(view) == ()
    await manager.stop()


@pytest.mark.parametrize("kind", ["deny", "exception", "invalid", "timeout", "cancelled"])
async def test_tool_checks_fail_closed_and_short_circuit_with_detached_requests(kind: str) -> None:
    calls: list[str] = []

    async def setup(api: SessionExtensionAPI) -> None:
        async def first(request: ToolPermissionRequest) -> PermissionDecision:
            request.arguments["nested"].append(2)
            calls.append("first")
            return PermissionDecision(True)

        async def gate(request: ToolPermissionRequest) -> PermissionDecision:
            calls.append("gate")
            assert request.arguments == {"nested": [1]}
            if kind == "deny":
                return PermissionDecision(False, "blocked")
            if kind == "exception":
                raise ValueError("checker error")
            if kind == "timeout":
                await forever()
            if kind == "cancelled":
                raise asyncio.CancelledError("callback cancelled")
            return cast(PermissionDecision, True)

        async def last(request: ToolPermissionRequest) -> PermissionDecision:
            calls.append("must not run")
            return PermissionDecision(True)

        api.register_tool_check(first)
        api.register_tool_check(gate)
        api.register_tool_check(last)

    manager = manager_for(Plugin(setup=setup), ("tool_check",), timeout=0.01)
    await manager.start()
    request = ToolPermissionRequest(Actions().view().binding, "host.tool", "id", {"nested": [1]})
    try:
        result = await manager.check_tool(request)
        assert result.allowed is False and calls == ["first", "gate"]
        assert request.arguments == {"nested": [1]}
        assert bool(manager.diagnostics) is (kind != "deny")
    finally:
        await manager.stop()


async def test_boundary_cumulative_append_only_with_initial_drafts_and_or_continuation() -> None:
    registry = ExtensionRegistry()
    actions = Actions()
    retained: list[BoundaryPreview] = []
    validations: list[tuple[CustomEntryDraft, ...]] = []

    async def first_setup(api: SessionExtensionAPI) -> None:
        async def boundary(preview: BoundaryPreview) -> BoundaryResult:
            assert [draft.custom_type for draft in preview.drafts] == ["host.initial"]
            preview.drafts[0].data["items"].append("must not escape")
            mutate_view(preview.view)
            return BoundaryResult((CustomEntryDraft("alpha.entry", {"items": [1]}),), True)

        api.register_boundary(boundary)

    async def second_setup(api: SessionExtensionAPI) -> None:
        async def boundary(preview: BoundaryPreview) -> BoundaryResult:
            assert [draft.custom_type for draft in preview.drafts] == [
                "host.initial",
                "alpha.entry",
            ]
            assert preview.drafts[0].data == {"items": []}
            assert message_text(preview.view) == "original"
            retained.append(preview)
            return BoundaryResult((CustomEntryDraft("beta.entry", 2),), False)

        api.register_boundary(boundary)

    registry.register(manifest("alpha", ("boundary",)), lambda: Plugin(setup=first_setup))
    registry.register(manifest("beta", ("boundary",)), lambda: Plugin("beta", setup=second_setup))
    manager = SessionExtensionManager(
        registry,
        actions=actions,
        enabled=("alpha", "beta"),
        grants={
            "alpha": frozenset({"boundary"}),
            "beta": frozenset({"boundary"}),
        },
    )
    await manager.start()
    initial = BoundaryPreview(actions.view(), (CustomEntryDraft("host.initial", {"items": []}),))

    def validate(drafts: tuple[CustomEntryDraft, ...]) -> BoundaryPreview:
        validations.append(deepcopy(drafts))
        return replace(initial, drafts=drafts)

    try:
        result = await manager.boundary(initial, validate)
        assert result.continue_ is True
        assert [draft.custom_type for draft in result.drafts] == [
            "host.initial",
            "alpha.entry",
            "beta.entry",
        ]
        assert [len(batch) for batch in validations] == [2, 3]
        retained[0].drafts[1].data["items"].append(99)
        assert result.drafts[1].data == {"items": [1]}
        assert initial.drafts[0].data == {"items": []}
        assert actions.staged == [] and actions.queued == []
    finally:
        await manager.stop()


@pytest.mark.parametrize(
    "kind", ["namespace", "invalid", "json", "exception", "validator", "timeout"]
)
async def test_boundary_failure_has_no_partial_batch_or_continuation(kind: str) -> None:
    calls: list[str] = []
    commits: list[CustomEntryDraft] = []
    continue_ = False

    async def setup(api: SessionExtensionAPI) -> None:
        async def first(preview: BoundaryPreview) -> BoundaryResult:
            calls.append("first")
            return BoundaryResult((CustomEntryDraft("alpha.good", 1),), True)

        async def broken(preview: BoundaryPreview) -> BoundaryResult:
            calls.append("broken")
            if kind == "namespace":
                return BoundaryResult((CustomEntryDraft("beta.stolen"),))
            if kind == "json":
                return BoundaryResult((CustomEntryDraft("alpha.bad", float("nan")),))
            if kind == "exception":
                raise RuntimeError("boundary failed")
            if kind == "timeout":
                await forever()
            if kind == "validator":
                return BoundaryResult((CustomEntryDraft("alpha.invalid", 2),))
            return cast(BoundaryResult, True)

        async def last(preview: BoundaryPreview) -> BoundaryResult:
            calls.append("last")
            return BoundaryResult()

        api.register_boundary(first)
        api.register_boundary(broken)
        api.register_boundary(last)

    manager = manager_for(Plugin(setup=setup), ("boundary",), timeout=0.01)
    await manager.start()
    initial = BoundaryPreview(Actions().view(), (CustomEntryDraft("host.seed"),))

    def validate(drafts: tuple[CustomEntryDraft, ...]) -> BoundaryPreview:
        if any(draft.custom_type == "alpha.invalid" for draft in drafts):
            raise ValueError("transaction invalid")
        return replace(initial, drafts=drafts)

    try:
        with pytest.raises((RegistrationError, RuntimeError, ValueError, TimeoutError)):
            result = await manager.boundary(initial, validate)
            commits.extend(result.drafts)
            continue_ = result.continue_
        assert commits == [] and not continue_ and calls == ["first", "broken"]
        assert manager.diagnostics[-1].operation == "boundary"
    finally:
        await manager.stop()


@pytest.mark.parametrize(
    "authorization", ["deny", "error", "invalid", "timeout", "cancelled", "allow"]
)
async def test_commands_mandatory_authorization_fail_closed_and_remove_on_unload(
    authorization: str,
) -> None:
    calls: list[str] = []

    async def command(argument: str) -> str:
        calls.append(argument)
        return "reply:" + argument

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_command(CommandDefinition("alpha.run", "run", command))
        with pytest.raises(NamespaceError):
            api.register_command(CommandDefinition("beta.run", "run", command))
        with pytest.raises(RegistrationError):
            api.register_command(CommandDefinition("alpha.run", "duplicate", command))

    async def authorize(name: str) -> bool:
        assert name == "alpha.run"
        if authorization == "deny":
            return False
        if authorization == "error":
            raise ValueError("authorization down")
        if authorization == "invalid":
            return cast(bool, "yes")
        if authorization == "timeout":
            await forever()
        if authorization == "cancelled":
            raise asyncio.CancelledError()
        return True

    manager = manager_for(Plugin(setup=setup), ("commands",), timeout=0.01)
    await manager.start()
    if authorization == "allow":
        assert await manager.execute_command("alpha.run", "arg", authorize=authorize) == "reply:arg"
        assert calls == ["arg"]
    else:
        with pytest.raises(CommandAuthorizationError):
            await manager.execute_command("alpha.run", "arg", authorize=authorize)
        assert calls == []
    await manager.disable("alpha")
    with pytest.raises(CommandNotFoundError):
        await manager.execute_command("alpha.run", "arg", authorize=authorize)
    await manager.stop()


@pytest.mark.parametrize("mode", ["disable", "unregister"])
async def test_command_authorization_race_rechecks_owner_and_registration(mode: str) -> None:
    handles: list[RegistrationHandle] = []
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def command(argument: str) -> str:
        calls.append(argument)
        return argument

    async def setup(api: SessionExtensionAPI) -> None:
        handles.append(api.register_command(CommandDefinition("alpha.run", "run", command)))

    async def authorize(name: str) -> bool:
        entered.set()
        await release.wait()
        return True

    manager = manager_for(Plugin(setup=setup), ("commands",), timeout=1)
    await manager.start()
    task = asyncio.create_task(manager.execute_command("alpha.run", "arg", authorize=authorize))
    await entered.wait()
    if mode == "disable":
        await manager.disable("alpha")
    else:
        handles[0].dispose()
    release.set()
    with pytest.raises((ExtensionRevokedError, CommandNotFoundError)):
        await task
    assert calls == []
    await manager.stop()


@pytest.mark.parametrize("kind", ["invalid", "exception", "timeout"])
async def test_command_handler_result_validation_and_errors(kind: str) -> None:
    async def command(argument: str) -> str:
        if kind == "exception":
            raise ValueError("command failed")
        if kind == "timeout":
            await forever()
        return cast(str, 1)

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_command(CommandDefinition("alpha.run", "run", command))

    async def authorize(name: str) -> bool:
        return True

    manager = manager_for(Plugin(setup=setup), ("commands",), timeout=0.01)
    await manager.start()
    try:
        with pytest.raises((ValueError, TimeoutError)):
            await manager.execute_command("alpha.run", "arg", authorize=authorize)
        assert manager.diagnostics[-1].operation == "command"
    finally:
        await manager.stop()


async def test_cancelled_authorization_never_executes_command() -> None:
    entered = asyncio.Event()
    calls: list[str] = []

    async def command(argument: str) -> str:
        calls.append(argument)
        return argument

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_command(CommandDefinition("alpha.run", "run", command))

    async def authorize(name: str) -> bool:
        entered.set()
        await forever()
        return True

    manager = manager_for(Plugin(setup=setup), ("commands",))
    await manager.start()
    task = asyncio.create_task(manager.execute_command("alpha.run", "arg", authorize=authorize))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
    await manager.stop()


async def test_boundary_none_still_validates_cumulative_drafts_and_validator_cannot_rewrite() -> (
    None
):
    calls: list[tuple[CustomEntryDraft, ...]] = []

    async def boundary(preview: BoundaryPreview) -> None:
        return None

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_boundary(boundary)

    manager = manager_for(Plugin(setup=setup), ("boundary",))
    await manager.start()
    initial = BoundaryPreview(Actions().view(), (CustomEntryDraft("host.initial"),))

    def validate(drafts: tuple[CustomEntryDraft, ...]) -> BoundaryPreview:
        calls.append(drafts)
        return replace(initial, drafts=drafts)

    try:
        assert await manager.boundary(initial, validate) == BoundaryResult(initial.drafts)
        assert calls == [initial.drafts]
        with pytest.raises(RegistrationError, match="preserve"):
            await manager.boundary(initial, lambda drafts: replace(initial, drafts=()))
    finally:
        await manager.stop()


async def test_context_cancellation_propagates_and_timeout_never_reuses_old_result() -> None:
    block = False
    entered = asyncio.Event()

    async def provider(view: SessionView) -> tuple[ContextBlock, ...]:
        if block:
            entered.set()
            await forever()
        return (ContextBlock("source", "fresh"),)

    async def setup(api: SessionExtensionAPI) -> None:
        api.register_context_provider(provider)

    manager = manager_for(Plugin(setup=setup), ("context",), timeout=0.02)
    await manager.start()
    view = Actions().view()
    assert await manager.context_blocks(view) == (ContextBlock("source", "fresh"),)
    block = True
    task = asyncio.create_task(manager.context_blocks(view))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await manager.context_blocks(view) == ()
    assert "TimeoutError" in manager.diagnostics[-1].message
    await manager.stop()


def test_registry_rejects_invalid_enabled_sequence_and_noncallable_factory() -> None:
    registry = ExtensionRegistry()
    with pytest.raises(ManifestError, match="callable"):
        registry.register(manifest(), cast(Any, None))
    with pytest.raises(ManifestError, match="scope"):
        registry.resolve((), scope=cast(Any, "invalid"))
    for enabled in ("alpha", {"alpha"}, ["bad.id"], [1]):
        with pytest.raises(DependencyError):
            registry.resolve(cast(Any, enabled), scope="session")
    with pytest.raises(DependencyError):
        SessionExtensionManager(registry, actions=Actions(), enabled="alpha")
