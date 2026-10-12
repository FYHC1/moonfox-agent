import asyncio
import json

import pytest
from pydantic import BaseModel, ValidationError

from moonfox_agent.api import AgentTool, AgentToolResult
from moonfox_ai.api import FauxProvider, FauxResponse, Model, TextContent
from moonfox_app.api import (
    HostSettings,
    ModelRegistration,
    Principal,
    SessionSettings,
    create_application,
)
from moonfox_app.config.loader import EnvironmentSecrets, load_settings
from moonfox_app.security import AccessDenied, AuthenticationError, TokenAuth
from moonfox_app.services.errors import BudgetExceeded, ConflictError, ResyncRequired
from moonfox_app.services.events import EventBus
from moonfox_app.services.store import HostStore

OWNER = Principal("owner", True)
OTHER = Principal("other")
MODEL = Model("demo", "faux", "faux", max_tokens=100)


def host(tmp_path, faux, *, settings=None, **kwargs):
    return create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "Test")},
        defaults=HostSettings(defaults=settings or SessionSettings()),
        **kwargs,
    )


async def session(hub, title="one", request_id="create"):
    return (await hub.create_session(OWNER, title=title, request_id=request_id))["id"]


async def submit(hub, identity, text="hi", key="input"):
    return await hub.submit_input(OWNER, identity, text=text, request_id=key, idempotency_key=key)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 - bounded public-state observation
            await asyncio.sleep(0.005)


async def test_durable_receipt_fifo_stable_ids_and_reopen(tmp_path):
    faux = FauxProvider([FauxResponse.text("one", delay_ms=5), FauxResponse.text("two")])
    hub = host(tmp_path, faux)
    await hub.start()
    identity = await session(hub)
    first = await submit(hub, identity, key="first")
    second = await submit(hub, identity, text="second", key="second")
    assert first["status"] == second["status"] == "pending"
    before = await hub.snapshot(OWNER, identity)
    assert before["session"]["pending_count"] == 2
    subscription = hub.subscribe(OWNER, identity, epoch=before["epoch"], after_seq=before["seq"])
    subscription.close()  # Closing the browser subscription must not stop the actor.
    await hub.wait_for_idle(identity)
    snapshot = await hub.snapshot(OWNER, identity)
    assert [item["status"] for item in snapshot["inputs"]] == ["completed", "completed"]
    assert [message["text"] for message in snapshot["messages"]] == ["hi", "one", "second", "two"]
    assert len({message["id"] for message in snapshot["messages"]}) == 4
    await hub.aclose()
    recovered = host(tmp_path, FauxProvider([]))
    await recovered.start()
    restored = await recovered.snapshot(OWNER, identity)
    assert restored["messages"] == snapshot["messages"]
    assert restored["epoch"] != snapshot["epoch"]
    with pytest.raises(ResyncRequired):
        recovered.subscribe(OWNER, identity, epoch=snapshot["epoch"], after_seq=snapshot["seq"])
    await recovered.aclose()


async def test_idempotency_and_access_checks_happen_before_admission(tmp_path):
    faux = FauxProvider([FauxResponse.text("ok")])
    hub = host(tmp_path, faux)
    await hub.start()
    identity = await session(hub)
    assert await session(hub) == identity
    with pytest.raises(ConflictError):
        await session(hub, title="different")
    first = await submit(hub, identity)
    duplicate = await hub.submit_input(
        OWNER, identity, text="hi", request_id="retry", idempotency_key="input"
    )
    assert duplicate["input_id"] == first["input_id"]
    with pytest.raises(ConflictError):
        await submit(hub, identity, text="changed")
    assert await hub.list_sessions(OTHER) == []
    for operation in (
        hub.snapshot(OTHER, identity),
        hub.submit_input(OTHER, identity, text="x", request_id="x", idempotency_key="x"),
        hub.control(OTHER, identity, action="cancel", request_id="x"),
    ):
        with pytest.raises(AccessDenied):
            await operation
    with pytest.raises(AccessDenied):
        hub.subscribe(OTHER, identity, epoch=hub.events.epoch, after_seq=0)
    await hub.wait_for_idle()
    assert len(faux.requests) == 1
    await hub.aclose()


class Args(BaseModel):
    pass


async def test_cancel_prioritizes_running_work_and_cancels_pending_inputs(tmp_path):
    started, settled = asyncio.Event(), asyncio.Event()

    async def execute(call_id, args, signal, update):
        started.set()
        await signal.wait()
        settled.set()
        return AgentToolResult([TextContent("stopped")], is_error=True)

    tool = AgentTool("slow", "", "", Args, execute=execute)
    faux = FauxProvider([FauxResponse.tool_call("slow", {})])
    hub = host(tmp_path, faux, tools=(tool,), settings=SessionSettings(allowed_tools=("slow",)))
    await hub.start()
    identity = await session(hub)
    await submit(hub, identity)
    await started.wait()
    await submit(hub, identity, text="queued", key="queued")
    receipt = await hub.control(OWNER, identity, action="cancel", request_id="cancel")
    assert receipt["status"] == "cancelling"
    await hub.wait_for_idle()
    snapshot = await hub.snapshot(OWNER, identity)
    assert settled.is_set()
    assert [item["status"] for item in snapshot["inputs"]] == ["aborted", "aborted"]
    assert not any(message["text"] == "queued" for message in snapshot["messages"])
    assert len(faux.requests) == 1
    await hub.aclose()


async def test_configuration_cas_and_hot_change_apply_to_next_run(tmp_path):
    first = FauxProvider(
        [FauxResponse.text("ok", delay_ms=20, chunk_size=1), FauxResponse.text("next")]
    )
    hub = host(tmp_path, first)
    await hub.start()
    identity = await session(hub)
    await submit(hub, identity)
    await until(lambda: len(first.requests) == 1)
    current = await hub.snapshot(OWNER, identity)
    settings = SessionSettings.model_validate_json(json.dumps(current["settings"]))
    changed = settings.model_copy(update={"thinking_level": "high", "max_turns_per_run": 1})
    result = await hub.update_session_config(OWNER, identity, settings=changed, expected_revision=1)
    assert result["revision"] == 2
    with pytest.raises(ConflictError):
        await hub.update_session_config(OWNER, identity, settings=changed, expected_revision=1)
    await hub.wait_for_idle()
    await submit(hub, identity, key="next")
    await hub.wait_for_idle()
    assert first.requests[0][2].reasoning == "off"
    assert first.requests[1][2].reasoning == "high"
    with pytest.raises(AccessDenied):
        await hub.config(OTHER)
    await hub.aclose()


async def test_continue_reuses_logical_budget_and_never_resets_limits(tmp_path):
    async def execute(call_id, args, signal, update):
        return AgentToolResult([TextContent("done")])

    faux = FauxProvider(
        [FauxResponse.tool_call("t", {}, "c1"), FauxResponse.tool_call("t", {}, "c2")]
    )
    hub = host(
        tmp_path,
        faux,
        settings=SessionSettings(
            max_turns_per_run=1, max_requests_per_input=2, allowed_tools=("t",)
        ),
        tools=(AgentTool("t", "", "", Args, execute=execute),),
    )
    await hub.start()
    identity = await session(hub)
    first = await submit(hub, identity)
    await hub.wait_for_idle()
    snapshot = await hub.snapshot(OWNER, identity)
    assert snapshot["inputs"][-1]["status"] == "limit_reached"
    second = await hub.control(
        OWNER, identity, action="continue", input_id=first["input_id"], request_id="continue"
    )
    await hub.wait_for_idle()
    snapshot = await hub.snapshot(OWNER, identity)
    assert snapshot["inputs"][0]["logical_id"] == snapshot["inputs"][1]["logical_id"]
    assert snapshot["inputs"][-1]["requests_used"] == 2
    with pytest.raises(BudgetExceeded):
        await hub.control(
            OWNER, identity, action="continue", input_id=second["input_id"], request_id="again"
        )
    assert len(faux.requests) == 2
    await hub.aclose()


@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
async def test_approvals_are_scoped_single_use_and_cancel_fail_closed(tmp_path, decision):
    executed = []

    async def execute(call_id, args, signal, update):
        executed.append(True)
        return AgentToolResult([TextContent("done")])

    faux = FauxProvider([FauxResponse.tool_call("t", {}), FauxResponse.text("end")])
    hub = host(
        tmp_path,
        faux,
        settings=SessionSettings(allowed_tools=("t",), approval_tools=("t",)),
        tools=(AgentTool("t", "", "", Args, execute=execute),),
    )
    await hub.start()
    identity = await session(hub)
    await submit(hub, identity)
    async with asyncio.timeout(3):
        while not (snapshot := await hub.snapshot(OWNER, identity))["approvals"]:  # noqa: ASYNC110
            await asyncio.sleep(0.005)
    approval = snapshot["approvals"][0]
    with pytest.raises(AccessDenied):
        await hub.resolve_approval(OTHER, identity, approval["id"], decision="approve")
    if decision == "cancel":
        await hub.control(OWNER, identity, action="cancel", request_id="cancel")
    else:
        await hub.resolve_approval(OWNER, identity, approval["id"], decision=decision)
    await hub.wait_for_idle()
    assert bool(executed) == (decision == "approve")
    with pytest.raises(ConflictError):
        await hub.resolve_approval(OWNER, identity, approval["id"], decision="approve")
    await hub.aclose()


async def test_event_replay_slow_subscriber_and_detached_snapshots():
    bus = EventBus(window=4, capacity=2)
    slow = bus.subscribe("s", epoch=bus.epoch, after_seq=0)
    fast = bus.subscribe("s", epoch=bus.epoch, after_seq=0)
    for index in range(3):
        event = bus.publish("s", "input_received", {"x": index})
        event["payload"]["x"] = -1
        assert (await fast.next())["payload"]["x"] == index
    with pytest.raises(ResyncRequired):
        await slow.next()
    replay = bus.subscribe("s", epoch=bus.epoch, after_seq=2)
    assert (await replay.next())["seq"] == 3
    with pytest.raises(ResyncRequired):
        bus.subscribe("s", epoch="other", after_seq=0)
    with pytest.raises(ResyncRequired):
        bus.subscribe("s", epoch=bus.epoch, after_seq=9)
    bus.close()
    with pytest.raises(ResyncRequired):
        await fast.next()


def test_host_store_recovery_keeps_pending_marks_started_unknown_and_locks(tmp_path):
    store = HostStore(tmp_path)
    record = store.create_session(
        owner_id="owner",
        domain="agent",
        title="one",
        request_id="create",
        settings=SessionSettings(),
    )
    one, _ = store.enqueue(record, text="one", request_id="one", key="one")
    two, _ = store.enqueue(record, text="two", request_id="two", key="two")
    store.claim(one.id, baseline_id="initial")
    with pytest.raises(ConflictError):
        HostStore(tmp_path)
    store.close()
    store = HostStore(tmp_path)
    store.recover()
    assert store.input(one.id).status == "unknown"
    assert store.input(two.id).status == "pending"
    store.close()


async def test_recovery_never_replays_unknown_input_and_missing_history_is_not_erased(tmp_path):
    faux = FauxProvider([FauxResponse.text("original")])
    hub = host(tmp_path, faux)
    await hub.start()
    identity = await session(hub)
    await submit(hub, identity)
    await hub.wait_for_idle()
    await hub.aclose()
    store = HostStore(tmp_path)
    item, _ = store.enqueue(
        store.session(identity), text="unknown", request_id="unknown", key="unknown"
    )
    store.claim(item.id, baseline_id="x")
    store.close()
    faux = FauxProvider([])
    hub = host(tmp_path, faux)
    await hub.start()
    await hub.wait_for_idle()
    assert (await hub.snapshot(OWNER, identity))["inputs"][-1]["status"] == "unknown"
    assert faux.requests == []
    await hub.aclose()
    path = tmp_path / "sessions" / "agent" / f"{identity}.jsonl"
    path.unlink()
    hub = host(tmp_path, FauxProvider([]))
    await hub.start()
    with pytest.raises(ConflictError):
        await hub.snapshot(OWNER, identity)
    assert not path.exists()
    await hub.aclose()


async def test_parallel_sessions_share_provider_concurrency_limit(tmp_path):
    faux = FauxProvider([FauxResponse.text("a", delay_ms=30), FauxResponse.text("b", delay_ms=30)])
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(MODEL, faux.stream, "faux")},
        defaults=HostSettings(max_concurrent_requests=1),
    )
    await hub.start()
    one, two = await session(hub), await session(hub, request_id="two")
    await submit(hub, one)
    await submit(hub, two)
    await until(lambda: len(faux.requests) >= 1)
    assert len(faux.requests) == 1
    await hub.wait_for_idle()
    assert len(faux.requests) == 2
    assert (await hub.snapshot(OWNER, one))["messages"][-1]["text"] == "a"
    assert (await hub.snapshot(OWNER, two))["messages"][-1]["text"] == "b"
    await hub.aclose()


def test_configuration_and_secret_validation(tmp_path):
    with pytest.raises(ValidationError):
        SessionSettings(max_turns_per_run=True)
    with pytest.raises(ValidationError):
        SessionSettings(approval_tools=("not-allowed",))
    with pytest.raises(ValidationError):
        SessionSettings(max_turns_per_run=3, max_requests_per_input=2)
    secrets = EnvironmentSecrets({"KEY": "test-secret"})
    assert secrets.resolve("env:KEY") == "test-secret"
    with pytest.raises(ValueError):
        secrets.resolve("file:secret.txt")
    token = "a" * 32
    auth = TokenAuth({token: OWNER})
    assert auth.authenticate(token) == OWNER
    with pytest.raises(AuthenticationError):
        auth.authenticate("wrong")
    config = tmp_path / "config.json"
    config.write_text(HostSettings().model_dump_json(), encoding="utf-8")
    assert load_settings(config) == HostSettings()
