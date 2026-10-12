import asyncio
from dataclasses import replace

import pytest

from moonfox_ai.api import FauxProvider, FauxResponse, Model, ThinkingContent, now_ms
from moonfox_app.api import HostSettings, ModelRegistration, Principal, create_application
from moonfox_app.extension_api import ChannelControl, ChannelInput
from moonfox_app.security import AccessDenied
from moonfox_app.services.channel import ChannelBinding
from moonfox_app.services.errors import ConflictError
from moonfox_app.services.store import HostStore

OWNER = Principal("owner", True)
BINDING = ChannelBinding("authorized", "bot-1", "owner", frozenset({"group:one", "user:one"}))


async def build(tmp_path, responses):
    faux = FauxProvider(responses)
    hub = create_application(
        home=tmp_path,
        models={"demo": ModelRegistration(Model("demo", "faux", "faux"), faux.stream, "faux")},
    )
    await hub.start()
    return hub, faux


def message(target="group:one", external="same"):
    return ChannelInput("authorized", "bot-1", target, "speaker", external, now_ms(), "hi")


async def wait(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 - bounded transport-state observation
            await asyncio.sleep(0.005)


async def test_channel_binding_routes_deduplicates_and_web_cannot_inject(tmp_path):
    hub, faux = await build(
        tmp_path, [FauxResponse.text("public reply"), FauxResponse.text("private reply")]
    )
    gateway = hub.bind_channel(BINDING)
    received = []

    async def send(reply):
        received.append(reply)

    gateway.subscribe_replies(send)
    one = await gateway.submit(message())
    duplicate = await gateway.submit(message())
    two = await gateway.submit(message("user:one"))
    assert one == duplicate and one.session_id != two.session_id
    with pytest.raises(AccessDenied):
        await gateway.submit(replace(message(), bot_account="bot-2"))
    with pytest.raises(AccessDenied):
        await gateway.submit(replace(message(), connection_id="spoofed"))
    with pytest.raises(AccessDenied):
        await gateway.submit(message("group:unauthorized"))
    with pytest.raises(AccessDenied):
        await hub.submit_input(
            OWNER, one.session_id, text="web message", request_id="web", idempotency_key="web"
        )
    await hub.wait_for_idle()
    await wait(lambda: len(received) == 2)
    assert {reply.text for reply in received} == {"public reply", "private reply"}
    snapshot = await hub.snapshot(OWNER, one.session_id)
    assert snapshot["session"]["domain"] == "qq" and "prompt" not in snapshot["capabilities"]
    agent = await hub.create_session(OWNER, title="same", request_id="agent")
    assert agent["id"] not in (one.session_id, two.session_id)
    assert (await hub.snapshot(OWNER, agent["id"]))["messages"] == []
    assert len(faux.requests) == 2
    await hub.aclose()


async def test_outbox_timeout_unknown_is_not_resent_after_restart(tmp_path):
    hub, _ = await build(tmp_path, [FauxResponse.text("one")])
    gateway = hub.bind_channel(BINDING, delivery_timeout=0.01)
    attempts = []

    async def failed(reply):
        attempts.append(reply)
        await asyncio.Event().wait()

    gateway.subscribe_replies(failed)
    receipt = await gateway.submit(message())
    await hub.wait_for_idle()
    await wait(lambda: len(attempts) == 1)
    await asyncio.sleep(0.03)
    await hub.aclose()
    store = HostStore(tmp_path)
    assert store.outbox(receipt.session_id)[0]["status"] == "unknown"
    with pytest.raises(ValueError):
        store.set_delivery(
            store.outbox(receipt.session_id)[0]["id"], expected="unknown", status="pending"
        )
    store.close()
    hub, _ = await build(tmp_path, [])
    gateway = hub.bind_channel(BINDING)
    gateway.subscribe_replies(failed)
    await asyncio.sleep(0.03)
    assert len(attempts) == 1
    await hub.aclose()


async def test_final_public_reply_only_no_reasoning_and_unsubscribe_pauses_delivery(tmp_path):
    response = FauxResponse.text("final")
    response.content.insert(0, ThinkingContent("secret reasoning"))
    hub, _ = await build(tmp_path, [response])
    gateway = hub.bind_channel(BINDING)
    delivered = []

    async def send(reply):
        delivered.append(reply)

    handle = gateway.subscribe_replies(send)
    handle.dispose()
    receipt = await gateway.submit(message())
    await hub.wait_for_idle()
    assert delivered == []
    gateway.subscribe_replies(send)
    await wait(lambda: bool(delivered))
    assert delivered[0].text == "final"
    assert "secret" not in str(delivered)
    await gateway.control(ChannelControl("authorized", "group:one", "cancel"))
    await hub.aclose()
    store = HostStore(tmp_path)
    assert store.outbox(receipt.session_id)[0]["status"] == "sent"
    store.close()


async def test_scope_columns_and_routes_are_structural(tmp_path):
    store = HostStore(tmp_path, defaults=HostSettings())
    settings = store.config()[1].defaults
    agent = store.create_session(
        owner_id="o",
        domain="agent",
        route_key="same",
        title="one",
        request_id="a",
        settings=settings,
    )
    qq = store.create_session(
        owner_id="o", domain="qq", route_key="same", title="one", request_id="q", settings=settings
    )
    assert agent.id != qq.id and agent.scope_id != qq.scope_id
    with pytest.raises(ConflictError):
        store.create_session(
            owner_id="another",
            domain="qq",
            route_key="same",
            title="one",
            request_id="q",
            settings=settings,
        )
    store.close()
