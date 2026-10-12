"""Existing sessions and reply subscriptions survive unrelated default changes."""

from dataclasses import replace

import pytest

from moonfox_ai.api import FauxProvider, FauxResponse, Model
from moonfox_app.api import ModelRegistration, Principal, create_application
from moonfox_app.config.providers import ModelSettings, ProviderSettings
from moonfox_app.extension_api import ChannelControl, ChannelInput
from moonfox_app.services.channel import ChannelBinding
from moonfox_app.services.errors import InvalidConfiguration

OWNER = Principal("owner", True)
BINDING = ChannelBinding("connection", "bot", "owner", frozenset({"group"}))


async def application(home):
    faux = FauxProvider([FauxResponse.text("reply")])
    hub = create_application(
        home=home,
        models={"demo": ModelRegistration(Model("demo", "faux", "faux"), faux.stream, "Demo")},
    )
    await hub.start()
    return hub, faux


async def disable_new_default(hub):
    provider = ProviderSettings(
        name="Managed", base_url="https://example.test/v1", auth_mode="none"
    )
    await hub.put_provider(OWNER, "managed", settings=provider, expected_revision=0)
    await hub.put_model(
        OWNER,
        "managed-model",
        settings=ModelSettings(provider_id="managed", name="Managed model", remote_id="remote"),
        expected_revision=0,
    )
    config = await hub.config(OWNER)
    await hub.set_default_model(OWNER, "managed-model", expected_revision=config["revision"])
    await hub.put_provider(
        OWNER,
        "managed",
        settings=provider.model_copy(update={"enabled": False}),
        expected_revision=1,
    )


async def test_existing_channel_ignores_unavailable_default_but_new_route_rejects(tmp_path):
    hub, faux = await application(tmp_path)
    try:
        original = hub.channel_session(BINDING, "group")
        await disable_new_default(hub)
        gateway = hub.bind_channel(BINDING)
        received = []

        async def send(reply):
            received.append(reply)

        gateway.subscribe_replies(send)
        assert hub.channel_session(BINDING, "group").id == original.id
        await gateway.control(ChannelControl("connection", "group", "cancel"))
        receipt = await gateway.submit(
            ChannelInput("connection", "bot", "group", "speaker", "one", 1, "hi")
        )
        await hub.wait_for_idle()
        assert receipt.session_id == original.id
        assert len(faux.requests) == 1
        assert (await hub.snapshot(OWNER, original.id))["inputs"][-1]["status"] == "completed"
        with pytest.raises(InvalidConfiguration):
            hub.channel_session(replace(BINDING, targets=frozenset({"new"})), "new")
        assert len(await hub.list_sessions(OWNER)) == 1
    finally:
        await hub.aclose()


async def test_create_session_retry_survives_unavailable_defaults(tmp_path):
    hub, _ = await application(tmp_path)
    try:
        original = await hub.create_session(OWNER, title="First", request_id="same")
        await disable_new_default(hub)
        duplicate = await hub.create_session(OWNER, title="First", request_id="same")
        assert duplicate["id"] == original["id"]
        with pytest.raises(InvalidConfiguration):
            await hub.create_session(OWNER, title="New", request_id="new")
        assert len(await hub.list_sessions(OWNER)) == 1
    finally:
        await hub.aclose()


async def test_reply_subscription_can_retry_after_routing_failure(tmp_path):
    hub, _ = await application(tmp_path)
    try:
        await disable_new_default(hub)
        gateway = hub.bind_channel(BINDING)

        async def send(reply):
            pass

        with pytest.raises(InvalidConfiguration):
            gateway.subscribe_replies(send)
        config = await hub.config(OWNER)
        await hub.set_default_model(OWNER, "demo", expected_revision=config["revision"])
        handle = gateway.subscribe_replies(send)
        handle.dispose()
    finally:
        await hub.aclose()
