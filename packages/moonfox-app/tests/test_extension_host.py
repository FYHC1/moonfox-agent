"""Host extensions use only the injected, revocable channel and task capabilities."""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from moonfox_app.extension_api import (
    ChannelControl,
    ChannelGateway,
    ChannelInput,
    ExtensionManifest,
    HostExtensionAPI,
    InputReceipt,
    RegistrationHandle,
    ReplyEnvelope,
    TaskHandle,
)
from moonfox_app.extensions import (
    CapabilityError,
    DependencyError,
    ExtensionRegistry,
    ExtensionRevokedError,
    HostExtensionManager,
    LifecycleError,
    RegistrationError,
)


class Handle:
    def __init__(self, dispose: Callable[[], None]) -> None:
        self.release = dispose

    def dispose(self) -> None:
        self.release()


class Gateway:
    def __init__(self, *, fail_dispose: bool = False) -> None:
        self.listeners: list[Callable[[ReplyEnvelope], Awaitable[None]]] = []
        self.submitted: list[ChannelInput] = []
        self.controls: list[ChannelControl] = []
        self.disposed = 0
        self.fail_dispose = fail_dispose
        self.receipt = InputReceipt("input", "session")

    async def submit(self, message: ChannelInput) -> InputReceipt:
        self.submitted.append(message)
        return self.receipt

    async def control(self, command: ChannelControl) -> None:
        self.controls.append(command)

    def subscribe_replies(
        self, listener: Callable[[ReplyEnvelope], Awaitable[None]]
    ) -> RegistrationHandle:
        self.listeners.append(listener)

        def dispose() -> None:
            self.disposed += 1
            if self.fail_dispose:
                raise RuntimeError("gateway unsubscribe failed")
            self.listeners.remove(listener)

        return Handle(dispose)

    async def emit(self, reply: ReplyEnvelope) -> None:
        for listener in tuple(self.listeners):
            await listener(reply)


class Plugin:
    def __init__(self, name: str = "bridge", log: list[str] | None = None) -> None:
        self.name = name
        self.log = [] if log is None else log
        self.api: HostExtensionAPI

    async def setup(self, api: HostExtensionAPI) -> None:
        self.api = api
        self.log.append(self.name + ".setup")

    async def start(self) -> None:
        self.log.append(self.name + ".start")

    async def stop(self) -> None:
        self.log.append(self.name + ".stop")

    async def dispose(self) -> None:
        self.log.append(self.name + ".dispose")


def manifest(name: str = "bridge", requires: tuple[str, ...] = ()) -> ExtensionManifest:
    return ExtensionManifest(name, "1", ">=0.1,<0.2", "host", ("tasks", "channel"), requires)


def grants(*names: str) -> dict[str, frozenset[str]]:
    return {name: frozenset({"tasks", "channel"}) for name in names}


def channel_input() -> ChannelInput:
    return ChannelInput("connection", "bot", "target", "speaker", "external", 1, "content")


def reply() -> ReplyEnvelope:
    return ReplyEnvelope("run", "message", "target", "reply")


async def forever() -> None:
    await asyncio.Event().wait()


async def test_host_setup_registration_only_active_forwarding_and_revocation() -> None:
    gateway = Gateway()
    calls: list[str] = []
    subscriptions: list[RegistrationHandle] = []
    retained_channel: list[ChannelGateway] = []
    handles: list[TaskHandle] = []

    class Bridge(Plugin):
        async def setup(self, api: HostExtensionAPI) -> None:
            await super().setup(api)
            retained_channel.append(api.channel)
            with pytest.raises(LifecycleError):
                await api.channel.submit(channel_input())
            with pytest.raises(LifecycleError):
                await api.channel.control(ChannelControl("connection", "target", "cancel"))
            with pytest.raises(LifecycleError):
                api.tasks.submit("setup", forever)

            async def receive(envelope: ReplyEnvelope) -> None:
                calls.append(envelope.text)

            subscriptions.append(api.channel.subscribe_replies(receive))
            await gateway.emit(reply())
            assert calls == []  # Subscription is declared but callbacks are not active yet.

        async def start(self) -> None:
            await super().start()
            receipt = await self.api.channel.submit(channel_input())
            assert receipt == gateway.receipt and receipt is not gateway.receipt
            await self.api.channel.control(ChannelControl("connection", "target", "cancel"))
            handles.append(self.api.tasks.submit("background", forever))

    plugin = Bridge()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = HostExtensionManager(
        registry, channel=gateway, enabled=("bridge",), grants=grants("bridge")
    )
    await manager.start()
    await manager.start()
    assert manager.active_ids == ("bridge",)
    assert gateway.submitted == [channel_input()]
    assert gateway.controls == [ChannelControl("connection", "target", "cancel")]
    await gateway.emit(reply())
    assert calls == ["reply"]
    old_callback = gateway.listeners[0]
    await manager.stop()
    await manager.stop()
    assert gateway.listeners == [] and gateway.disposed == 1
    subscriptions[0].dispose()
    assert gateway.disposed == 1
    await old_callback(reply())
    assert calls == ["reply"]
    with pytest.raises(ExtensionRevokedError):
        await retained_channel[0].submit(channel_input())
    with pytest.raises(ExtensionRevokedError):
        await retained_channel[0].control(ChannelControl("connection", "target", "cancel"))
    with pytest.raises(ExtensionRevokedError):
        retained_channel[0].subscribe_replies(old_callback)
    with pytest.raises(ExtensionRevokedError):
        _ = plugin.api.channel
    with pytest.raises(asyncio.CancelledError):
        await handles[0].wait()
    assert plugin.log == ["bridge.setup", "bridge.start", "bridge.stop", "bridge.dispose"]


async def test_host_missing_channel_and_missing_grants_preflight_prevents_factory() -> None:
    calls: list[str] = []

    def factory() -> Plugin:
        calls.append("factory")
        return Plugin()

    registry = ExtensionRegistry()
    registry.register(manifest(), factory)
    missing_channel = HostExtensionManager(registry, enabled=("bridge",), grants=grants("bridge"))
    missing_grants = HostExtensionManager(registry, channel=Gateway(), enabled=("bridge",))
    for manager in (missing_channel, missing_grants):
        with pytest.raises(CapabilityError):
            await manager.start()
    assert calls == []


async def test_host_no_requested_capability_is_not_granted_by_accident() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(ExtensionManifest("bridge", "1", "==0.1.0", "host"), lambda: plugin)
    manager = HostExtensionManager(
        registry, channel=Gateway(), enabled=("bridge",), grants=grants("bridge")
    )
    await manager.start()
    with pytest.raises(CapabilityError):
        _ = plugin.api.channel
    with pytest.raises(CapabilityError):
        _ = plugin.api.tasks
    await manager.stop()


async def test_host_reply_snapshots_isolate_errors_and_timeout_and_unsubscribe() -> None:
    gateway = Gateway()
    seen: list[str] = []
    subscriptions: list[RegistrationHandle] = []

    class Bridge(Plugin):
        async def setup(self, api: HostExtensionAPI) -> None:
            await super().setup(api)

            async def first(envelope: ReplyEnvelope) -> None:
                object.__setattr__(envelope, "text", "mutated")
                raise RuntimeError("reply failure")

            async def second(envelope: ReplyEnvelope) -> None:
                seen.append(envelope.text)

            async def timeout(envelope: ReplyEnvelope) -> None:
                await forever()

            api.channel.subscribe_replies(first)
            subscriptions.append(api.channel.subscribe_replies(second))
            api.channel.subscribe_replies(timeout)

    registry = ExtensionRegistry()
    registry.register(manifest(), Bridge)
    manager = HostExtensionManager(
        registry, channel=gateway, enabled=("bridge",), grants=grants("bridge"), timeout=0.01
    )
    await manager.start()
    original = reply()
    await gateway.emit(original)
    assert original.text == "reply" and seen == ["reply"]
    assert len(manager.diagnostics) == 2
    assert {item.operation for item in manager.diagnostics} == {"channel.reply"}
    subscriptions[0].dispose()
    subscriptions[0].dispose()
    assert len(gateway.listeners) == 2 and gateway.disposed == 1
    await manager.stop()
    assert gateway.listeners == [] and gateway.disposed == 3


@pytest.mark.parametrize("phase", ["setup", "start", "cancel"])
async def test_host_partial_failure_or_cancellation_reverse_cleanup(phase: str) -> None:
    log: list[str] = []
    handles: list[TaskHandle] = []
    entered = asyncio.Event()
    original = ValueError("original host failure")
    gateway = Gateway()

    class Bridge(Plugin):
        async def setup(self, api: HostExtensionAPI) -> None:
            await super().setup(api)

            async def receive(envelope: ReplyEnvelope) -> None:
                pass

            api.channel.subscribe_replies(receive)
            if phase == "setup" and self.name == "second":
                raise original

        async def start(self) -> None:
            await super().start()
            handles.append(self.api.tasks.submit("background", forever))
            if self.name == "second":
                if phase == "start":
                    raise original
                if phase == "cancel":
                    entered.set()
                    await forever()

        async def stop(self) -> None:
            await super().stop()
            raise RuntimeError("stop failure")

        async def dispose(self) -> None:
            await super().dispose()
            raise RuntimeError("dispose failure")

    registry = ExtensionRegistry()
    registry.register(manifest("first"), lambda: Bridge("first", log))
    registry.register(manifest("second"), lambda: Bridge("second", log))
    manager = HostExtensionManager(
        registry, channel=gateway, enabled=("first", "second"), grants=grants("first", "second")
    )
    if phase == "cancel":
        task = asyncio.create_task(manager.start())
        await entered.wait()
        task.cancel("host cancelled")
        with pytest.raises(asyncio.CancelledError, match="host cancelled"):
            await task
    else:
        with pytest.raises(ValueError) as raised:
            await manager.start()
        assert raised.value is original
    assert manager.active_ids == () and gateway.listeners == []
    if phase == "setup":
        assert log == ["first.setup", "second.setup", "second.dispose", "first.dispose"]
    else:
        assert log[-4:] == ["second.stop", "second.dispose", "first.stop", "first.dispose"]
    for handle in handles:
        with pytest.raises(asyncio.CancelledError):
            await handle.wait()
    await manager.stop()


async def test_host_unsubscribe_failure_mutes_callbacks_and_releases_gateway() -> None:
    gateway = Gateway(fail_dispose=True)
    calls: list[str] = []
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = HostExtensionManager(
        registry, channel=gateway, enabled=("bridge",), grants=grants("bridge")
    )
    await manager.start()

    async def receive(envelope: ReplyEnvelope) -> None:
        calls.append(envelope.text)

    plugin.api.channel.subscribe_replies(receive)
    plugin.api.channel.subscribe_replies(receive)
    await manager.stop()
    await gateway.emit(reply())  # Broken upstream unsubscribe cannot revive plugin callbacks.
    assert calls == [] and gateway.disposed == 2 and len(manager.diagnostics) == 2
    reference = weakref.ref(gateway)
    del manager, gateway
    gc.collect()
    assert reference() is None


async def test_host_restart_fresh_instances_dependency_disable_and_scope_rejection() -> None:
    instances: list[Plugin] = []

    def factory(name: str) -> Plugin:
        plugin = Plugin(name)
        instances.append(plugin)
        return plugin

    registry = ExtensionRegistry()
    registry.register(manifest("bridge"), lambda: factory("bridge"))
    registry.register(manifest("dependent", ("bridge",)), lambda: factory("dependent"))
    registry.register(
        ExtensionManifest("session", "1", ">=0.1", "session"), lambda: factory("session")
    )
    invalid = HostExtensionManager(registry, enabled=("session",))
    with pytest.raises(DependencyError):
        await invalid.start()
    assert instances == []
    manager = HostExtensionManager(
        registry,
        channel=Gateway(),
        enabled=("dependent", "bridge"),
        grants=grants("bridge", "dependent"),
    )
    await asyncio.gather(manager.start(), manager.start())
    assert len(instances) == 2 and tuple(manager.active_ids) == ("bridge", "dependent")
    with pytest.raises(DependencyError):
        await manager.disable("bridge")
    await manager.stop()
    await manager.start()
    assert len(instances) == 4 and instances[0] is not instances[2]
    await manager.disable("dependent")
    await manager.disable("bridge")
    assert tuple(manager.active_ids) == ()
    await manager.stop()


async def test_host_channel_validates_input_control_and_explicit_timeout() -> None:
    class SlowGateway(Gateway):
        async def submit(self, message: ChannelInput) -> InputReceipt:
            await forever()
            return self.receipt

    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = HostExtensionManager(
        registry, channel=SlowGateway(), enabled=("bridge",), grants=grants("bridge"), timeout=0.01
    )
    await manager.start()
    try:
        with pytest.raises(RegistrationError):
            await plugin.api.channel.submit(cast(Any, "spoof"))
        with pytest.raises(RegistrationError):
            await plugin.api.channel.control(cast(Any, "spoof"))
        with pytest.raises(TimeoutError):
            await plugin.api.channel.submit(channel_input())
    finally:
        await manager.stop()
