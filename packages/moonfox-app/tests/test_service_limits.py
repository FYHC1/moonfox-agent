import asyncio
import json

import pytest
from pydantic import BaseModel

from moonfox_agent.api import AbortToken, AgentTool, AgentToolResult
from moonfox_ai.api import (
    Context,
    FauxProvider,
    FauxResponse,
    Model,
    StreamOptions,
    TextContent,
    Usage,
    normalize_context,
)
from moonfox_app.api import (
    ExtensionManifest,
    ExtensionRegistry,
    HostSettings,
    ModelRegistration,
    Principal,
    SessionSettings,
    create_application,
)
from moonfox_app.extension_api import PromptSection
from moonfox_app.services.errors import BudgetExceeded
from moonfox_app.services.gateway import ModelGateway
from moonfox_app.services.store import HostStore

OWNER = Principal("owner", True)
MODEL = Model("demo", "faux", "faux", max_tokens=100)


async def test_session_extension_instance_lives_across_inputs_and_reload_is_next_boundary(tmp_path):
    calls = []

    class Plugin:
        async def setup(self, api):
            calls.append("setup")
            api.register_prompt_section(self.section)

        async def attach(self, ctx):
            calls.append("attach")

        async def section(self, view):
            return (PromptSection("test.style", "hello"),)

        async def detach(self):
            calls.append("detach")

        async def dispose(self):
            calls.append("dispose")

    registry = ExtensionRegistry()
    manifest = ExtensionManifest("test", "0.1.0", ">=0.1,<0.2", "session", ("prompt",))
    registry.register(manifest, Plugin)
    faux = FauxProvider(
        [FauxResponse.text("one"), FauxResponse.text("two"), FauxResponse.text("three")]
    )
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "faux")},
        registry=registry,
        extension_manifests=(manifest,),
        extension_grants={"test": frozenset({"prompt"})},
        defaults=HostSettings(defaults=SessionSettings(enabled_extensions=("test",))),
    )
    await hub.start()
    identity = (await hub.create_session(OWNER, title="x", request_id="new"))["id"]
    for key in ("one", "two"):
        await hub.submit_input(OWNER, identity, text=key, request_id=key, idempotency_key=key)
        await hub.wait_for_idle()
    assert calls == ["setup", "attach"]
    snapshot = await hub.snapshot(OWNER, identity)
    settings = SessionSettings.model_validate_json(json.dumps(snapshot["settings"]))
    await hub.update_session_config(
        OWNER,
        identity,
        settings=settings.model_copy(update={"enabled_extensions": ()}),
        expected_revision=snapshot["config_revision"],
    )
    await hub.submit_input(
        OWNER, identity, text="three", request_id="three", idempotency_key="three"
    )
    await hub.wait_for_idle()
    assert calls == ["setup", "attach", "detach", "dispose"]
    await hub.aclose()


def meter(tmp_path, settings=None):
    store = HostStore(tmp_path)
    record = store.create_session(
        owner_id="owner",
        domain="agent",
        title="s",
        request_id="create",
        settings=settings or SessionSettings(),
    )
    item, _ = store.enqueue(record, text="x", request_id="x", key="x")
    return store, item, ModelGateway(store, concurrency=1)


async def test_token_admission_precedes_provider_and_usage_excess_blocks_next_call(tmp_path):
    store, item, gateway = meter(tmp_path, SessionSettings(max_tokens_per_input=3))
    faux = FauxProvider([])
    stream = gateway.bind(item.logical_id, faux.stream)(
        MODEL, normalize_context(Context([], system_prompt="x" * 100))
    )
    with pytest.raises(BudgetExceeded):
        await stream.result()
    await stream.aclose()
    assert faux.requests == [] and store.budget(item.logical_id).requests_used == 0
    store.close()

    store, item, gateway = meter(tmp_path / "excess", SessionSettings(max_tokens_per_input=200))
    faux = FauxProvider([FauxResponse.text("x", usage=Usage(total_tokens=300))])
    provider = gateway.bind(item.logical_id, faux.stream)
    first = provider(MODEL, normalize_context(Context([])))
    assert (await first.result()).stop_reason == "stop"
    await first.aclose()
    assert store.budget(item.logical_id).tokens_used == 300
    second = provider(MODEL, normalize_context(Context([])))
    with pytest.raises(BudgetExceeded):
        await second.result()
    await second.aclose()
    assert len(faux.requests) == 1
    store.close()


async def test_cancelling_queued_provider_does_not_invoke_or_charge_it(tmp_path):
    store, item, gateway = meter(tmp_path)
    faux = FauxProvider([FauxResponse.text("slow", delay_ms=80, chunk_size=1)])
    provider = gateway.bind(item.logical_id, faux.stream)
    first = provider(MODEL, normalize_context(Context([])))
    async with asyncio.timeout(2):
        while not faux.requests:  # noqa: ASYNC110 - observe public faux request capture
            await asyncio.sleep(0)
    token = AbortToken()
    second = provider(MODEL, normalize_context(Context([])), StreamOptions(signal=token))
    await asyncio.sleep(0)
    token.abort()
    assert (await second.result()).stop_reason == "aborted"
    await second.aclose()
    await first.result()
    await first.aclose()
    assert len(faux.requests) == 1
    assert store.budget(item.logical_id).requests_used == 1
    store.close()


async def test_upstream_observer_overflow_recovers_authoritative_result(tmp_path):
    store, item, gateway = meter(tmp_path)
    faux = FauxProvider([FauxResponse.text("x" * 3000, chunk_size=1)])
    stream = gateway.bind(item.logical_id, faux.stream)(MODEL, normalize_context(Context([])))
    result = await stream.result()
    assert result.content[0].text == "x" * 3000
    await stream.aclose()
    store.close()


async def test_shutdown_cancels_running_leaves_unclaimed_input_for_restart(tmp_path):
    started = asyncio.Event()

    class Args(BaseModel):
        pass

    async def execute(call_id, args, signal, update):
        started.set()
        await signal.wait()
        return AgentToolResult([TextContent("cancelled")], is_error=True)

    tool = AgentTool("slow", "", "", Args, execute=execute)
    faux = FauxProvider([FauxResponse.tool_call("slow", {})])
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "faux")},
        tools=(tool,),
        defaults=HostSettings(defaults=SessionSettings(allowed_tools=("slow",))),
    )
    await hub.start()
    identity = (await hub.create_session(OWNER, title="x", request_id="new"))["id"]
    await hub.submit_input(OWNER, identity, text="one", request_id="one", idempotency_key="one")
    await started.wait()
    await hub.submit_input(OWNER, identity, text="two", request_id="two", idempotency_key="two")
    close = asyncio.create_task(hub.aclose())
    await asyncio.sleep(0)
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    store = HostStore(tmp_path)
    assert [item.status for item in store.inputs(identity)] == ["aborted", "pending"]
    store.close()
    faux = FauxProvider([FauxResponse.text("recovered pending")])
    hub = create_application(
        home=tmp_path, models={"demo": ModelRegistration(MODEL, faux.stream, "faux")}, tools=(tool,)
    )
    await hub.start()
    await hub.wait_for_idle()
    assert len(faux.requests) == 1
    assert (await hub.snapshot(OWNER, identity))["inputs"][-1]["status"] == "completed"
    await hub.aclose()


async def test_run_timeout_and_approval_timeout_are_fail_closed(tmp_path):
    class Args(BaseModel):
        pass

    executed = []

    async def execute(call_id, args, signal, update):
        executed.append(True)
        return AgentToolResult([TextContent("no")])

    faux = FauxProvider([FauxResponse.tool_call("t", {})])
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "faux")},
        tools=(AgentTool("t", "", "", Args, execute=execute),),
        defaults=HostSettings(
            defaults=SessionSettings(
                allowed_tools=("t",), approval_tools=("t",), run_timeout_seconds=1
            ),
            approval_timeout_seconds=1,
        ),
    )
    await hub.start()
    identity = (await hub.create_session(OWNER, title="x", request_id="new"))["id"]
    await hub.submit_input(OWNER, identity, text="hi", request_id="one", idempotency_key="one")
    await hub.wait_for_idle()
    snapshot = await hub.snapshot(OWNER, identity)
    assert executed == []
    assert snapshot["inputs"][-1]["status"] in ("aborted", "error")
    assert snapshot["approvals"] == []
    await hub.aclose()
