import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel

from moonfox_agent.api import AgentTool, AgentToolResult, RunOptions
from moonfox_ai.api import (
    FauxProvider,
    FauxResponse,
    Model,
    TextContent,
    ToolResultMessage,
    UserMessage,
    get_current_system_prompt,
    get_current_tools,
)
from moonfox_app.api import (
    ExtensionManifest,
    ExtensionRegistry,
    InMemorySessionStore,
    JsonlSessionStore,
    SessionBinding,
    create_headless_app,
)
from moonfox_app.extension_api import (
    ContextBlock,
    CustomEntryDraft,
    PermissionDecision,
    PromptSection,
)
from moonfox_app.extensions import CapabilityError, ExtensionRevokedError
from moonfox_app.runtime.storage import StoreError

MODEL = Model("faux", "faux", "faux")
BINDING = SessionBinding("session", "scope")
CAPS = ("prompt", "context", "entries", "tools", "tasks")


class Args(BaseModel):
    pass


class Plugin:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def setup(self, api):
        self.api = api
        self.calls.append("setup")
        api.register_prompt_section(self.sections)
        api.register_context_provider(self.context)
        api.register_tool(AgentTool("demo.tool", "test", "test", Args, execute=self.execute))

    async def attach(self, ctx):
        self.ctx = ctx
        self.calls.append("attach")
        ctx.actions.stage_entries([CustomEntryDraft("demo.attached", {"yes": True})])
        ctx.tasks.submit("worker", self.work)

    async def work(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.finished.set()

    async def sections(self, view):
        return (PromptSection("demo.style", "friendly"),)

    async def context(self, view):
        return (ContextBlock("demo.source", "reference"),)

    async def execute(self, call_id, args, signal, update):
        self.calls.append("tool")
        return AgentToolResult([TextContent("ok")])

    async def detach(self):
        self.calls.append("detach")

    async def dispose(self):
        self.calls.append("dispose")


def registry(factory=Plugin):
    result = ExtensionRegistry()
    result.register(ExtensionManifest("demo", "0.1.0", ">=0.1,<0.2", "session", CAPS), factory)
    return result


async def make(faux, **kwargs):
    return await create_headless_app(
        store=kwargs.pop("store", InMemorySessionStore()),
        binding=BINDING,
        model=MODEL,
        stream_fn=faux.stream,
        **kwargs,
    )


async def test_real_extension_integration_unload_and_task_cleanup():
    instances = []

    def factory():
        plugin = Plugin()
        instances.append(plugin)
        return plugin

    faux = FauxProvider(
        [
            FauxResponse.tool_call("demo.tool", {}),
            FauxResponse.text("end"),
            FauxResponse.text("without plugin"),
        ]
    )

    async def permit(request):
        return PermissionDecision(True)

    app = await make(
        faux,
        registry=registry(factory),
        enabled_extensions=("demo",),
        grants={"demo": frozenset(CAPS)},
        permission_checker=permit,
        system_prompt="base",
    )
    plugin = instances[0]
    await plugin.started.wait()
    assert app.active_extensions == ("demo",)
    await app.prompt("hello", options=RunOptions(2))
    assert plugin.calls == ["setup", "attach", "tool"]
    assert get_current_system_prompt(faux.requests[0][1].messages) == "base\n\nfriendly"
    assert len(get_current_tools(app.view().messages)) == 1
    await app.disable_extension("demo")
    assert plugin.finished.is_set()
    assert plugin.calls[-2:] == ["detach", "dispose"]
    assert app.active_extensions == ()
    assert get_current_system_prompt(app.view().messages) == "base"
    assert get_current_tools(app.view().messages) == []
    with pytest.raises(ExtensionRevokedError):
        plugin.ctx.actions.stage_entries([CustomEntryDraft("demo.after", 1)])
    await app.prompt("again", options=RunOptions(1))
    assert "reference" not in str(faux.requests[-1][1].messages)
    await app.aclose()
    await app.aclose()


async def test_missing_grants_reject_enable_and_close_owned_store():
    store = InMemorySessionStore()
    with pytest.raises(CapabilityError):
        await make(FauxProvider([]), store=store, registry=registry(), enabled_extensions=("demo",))
    with pytest.raises(StoreError):
        store.snapshot()


async def test_failed_required_prompt_cleans_up_plugin_and_releases_file_lease(tmp_path):
    instances = []

    class BadPrompt(Plugin):
        async def sections(self, view):
            raise ValueError("prompt unavailable")

    def factory():
        plugin = BadPrompt()
        instances.append(plugin)
        return plugin

    path = tmp_path / "s.jsonl"
    with pytest.raises(ValueError, match="prompt unavailable"):
        await make(
            FauxProvider([]),
            store=JsonlSessionStore(path, create=True),
            registry=registry(factory),
            enabled_extensions=("demo",),
            grants={"demo": frozenset(CAPS)},
        )
    assert instances[0].calls[-2:] == ["detach", "dispose"]
    with JsonlSessionStore(path) as reopened:
        assert not any(e.type == "custom" for e in reopened.snapshot().entries)


async def test_two_sessions_get_distinct_instances_and_views():
    instances = []

    def factory():
        plugin = Plugin()
        instances.append(plugin)
        return plugin

    catalog = registry(factory)
    first = await make(
        FauxProvider([FauxResponse.text("private-one")]),
        registry=catalog,
        enabled_extensions=("demo",),
        grants={"demo": frozenset(CAPS)},
    )
    second = await make(
        FauxProvider([FauxResponse.text("private-two")]),
        registry=catalog,
        enabled_extensions=("demo",),
        grants={"demo": frozenset(CAPS)},
    )
    await asyncio.gather(
        first.prompt("one", options=RunOptions(1)), second.prompt("two", options=RunOptions(1))
    )
    assert instances[0] is not instances[1]
    assert "private-two" not in str(first.view().messages)
    assert "private-one" not in str(second.view().messages)
    await first.aclose()
    assert second.active_extensions == ("demo",)
    await second.aclose()


async def test_close_aborts_active_run_waits_and_prevents_new_input():
    started, released = asyncio.Event(), asyncio.Event()

    async def execute(call_id, args, signal, update):
        started.set()
        await signal.wait()
        released.set()
        return AgentToolResult([TextContent("stopped")], is_error=True)

    async def permit(request):
        return PermissionDecision(True)

    app = await make(
        FauxProvider([FauxResponse.tool_call("slow", {})]),
        tools=(AgentTool("slow", "", "", Args, execute=execute),),
        permission_checker=permit,
    )
    events = []
    app.subscribe(events.append)
    run = asyncio.create_task(app.prompt("run", options=RunOptions(2)))
    await started.wait()
    close = asyncio.create_task(app.aclose())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await app.prompt("blocked", options=RunOptions(1))
    await close
    assert (await run).reason == "aborted"
    assert released.is_set()
    assert events[-1].type == "agent_settled"


async def test_close_from_own_listener_is_rejected_without_poisoning_app():
    app = await make(FauxProvider([FauxResponse.text("one"), FauxResponse.text("two")]))
    seen = []

    async def listener(event):
        if event.type == "agent_settled":
            with pytest.raises(RuntimeError, match="own"):
                await app.aclose()
            seen.append(True)

    app.subscribe(listener)
    await app.prompt("one", options=RunOptions(1))
    await app.prompt("two", options=RunOptions(1))
    assert seen == [True, True]
    await app.aclose()


async def test_model_thinking_replay_and_branch_selection(tmp_path):
    path = tmp_path / "s.jsonl"
    app = await make(
        FauxProvider([FauxResponse.text("answer")]), store=JsonlSessionStore(path, create=True)
    )
    root = app.view().leaf_id
    model = replace(MODEL, id="new")
    app.set_model(model)
    app.set_thinking_level("high")
    await app.prompt("one", options=RunOptions(1))
    leaf = app.view().leaf_id
    await app.aclose()
    app = await make(FauxProvider([]), store=JsonlSessionStore(path))
    assert app.view().model == model and app.view().thinking_level == "high"
    app.select_leaf(root)
    assert app.view().model == MODEL and app.view().thinking_level == "off"
    app.select_leaf(leaf)
    assert app.view().model == model
    app.enqueue(UserMessage("later", 1), "follow_up")
    with pytest.raises(RuntimeError, match="pending"):
        app.select_leaf(root)
    await app.aclose()


async def test_permission_checks_validated_prepared_values_not_raw_model_arguments():
    checks = []
    executions = []

    class PathArgs(BaseModel):
        path: str

    async def prepare_check(request):
        checks.append(request.arguments)
        return PermissionDecision(request.arguments["path"] == "safe")

    async def execute(call_id, args, signal, update):
        executions.append(args.path)
        return AgentToolResult([TextContent("bad")])

    target = AgentTool(
        "path",
        "",
        "",
        PathArgs,
        execute=execute,
        prepare_arguments=lambda raw: {"path": "forbidden"},
    )
    app = await make(
        FauxProvider([FauxResponse.tool_call("path", {"path": "safe"})]),
        tools=(target,),
        permission_checker=prepare_check,
    )
    await app.prompt("run", options=RunOptions(1))
    assert checks == [{"path": "forbidden"}]
    assert executions == []
    assert next(m for m in app.view().messages if isinstance(m, ToolResultMessage)).is_error
    await app.aclose()
