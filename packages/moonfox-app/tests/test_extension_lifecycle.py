"""Lifecycle rollback, revocation, isolation, and owned-task cancellation contracts."""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import Sequence

import pytest

from moonfox_ai.api import Model, UserMessage
from moonfox_app.extension_api import (
    CustomEntryDraft,
    Delivery,
    ExtensionManifest,
    SessionBinding,
    SessionExtensionAPI,
    SessionExtensionContext,
    SessionView,
    TaskHandle,
)
from moonfox_app.extensions import (
    DependencyError,
    ExtensionRegistry,
    ExtensionRevokedError,
    LifecycleError,
    SessionExtensionManager,
)


class Actions:
    def __init__(self, name: str = "one") -> None:
        self.name = name
        self.queued: list[UserMessage] = []

    def view(self) -> SessionView:
        return SessionView(
            SessionBinding(self.name, "opaque"), 1, "leaf", (), Model("f", "f", "f"), "off"
        )

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None:
        pass

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        self.queued.append(message)


class Plugin:
    def __init__(self, name: str = "alpha", log: list[str] | None = None) -> None:
        self.name = name
        self.log = [] if log is None else log
        self.api: SessionExtensionAPI
        self.ctx: SessionExtensionContext

    async def setup(self, api: SessionExtensionAPI) -> None:
        self.api = api
        self.log.append(self.name + ".setup")

    async def attach(self, ctx: SessionExtensionContext) -> None:
        self.ctx = ctx
        self.log.append(self.name + ".attach")

    async def detach(self) -> None:
        self.log.append(self.name + ".detach")

    async def dispose(self) -> None:
        self.log.append(self.name + ".dispose")


def manifest(name: str = "alpha", requires: tuple[str, ...] = ()) -> ExtensionManifest:
    return ExtensionManifest(
        name, "1", ">=0.1,<0.2", "session", ("tasks", "events", "enqueue"), requires
    )


def grants(*names: str) -> dict[str, frozenset[str]]:
    return {name: frozenset({"tasks", "events", "enqueue"}) for name in names}


async def forever() -> None:
    await asyncio.Event().wait()


async def no_work() -> None:
    pass


async def test_lifecycle_serialized_idempotent_dependency_order_and_fresh_restarts() -> None:
    registry = ExtensionRegistry()
    log: list[str] = []
    instances: list[Plugin] = []

    def alpha() -> Plugin:
        plugin = Plugin("alpha", log)
        instances.append(plugin)
        return plugin

    def beta() -> Plugin:
        plugin = Plugin("beta", log)
        instances.append(plugin)
        return plugin

    registry.register(manifest("alpha"), alpha)
    registry.register(manifest("beta", ("alpha",)), beta)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("beta", "alpha"), grants=grants("alpha", "beta")
    )
    await asyncio.gather(manager.start(), manager.start())
    assert len(instances) == 2 and tuple(manager.active_ids) == ("alpha", "beta")
    assert log == ["alpha.setup", "beta.setup", "alpha.attach", "beta.attach"]
    with pytest.raises(DependencyError, match="required by beta"):
        await manager.disable("alpha")
    await asyncio.gather(manager.stop(), manager.stop())
    assert log[-4:] == ["beta.detach", "beta.dispose", "alpha.detach", "alpha.dispose"]
    await manager.start()
    assert len(instances) == 4 and instances[0] is not instances[2]
    with pytest.raises(ExtensionRevokedError):
        instances[0].api.enqueue(UserMessage("stale", 1), "steer")
    await manager.disable("beta")
    await manager.disable("beta")
    assert tuple(manager.active_ids) == ("alpha",)
    await manager.disable("alpha")
    assert tuple(manager.active_ids) == ()
    await manager.stop()
    await manager.start()
    assert len(instances) == 4 and tuple(manager.active_ids) == ()
    await manager.stop()


async def test_idle_guard_prevents_lifecycle_mutation_before_factory_and_while_active() -> None:
    busy = True
    calls: list[str] = []

    def ensure_idle() -> None:
        calls.append("guard")
        if busy:
            raise RuntimeError("session is running")

    def factory() -> Plugin:
        calls.append("factory")
        return Plugin()

    registry = ExtensionRegistry()
    registry.register(manifest(), factory)
    manager = SessionExtensionManager(
        registry,
        actions=Actions(),
        enabled=("alpha",),
        grants=grants("alpha"),
        ensure_idle=ensure_idle,
    )
    with pytest.raises(RuntimeError, match="session is running"):
        await manager.start()
    assert calls == ["guard"]
    busy = False
    await manager.start()
    busy = True
    with pytest.raises(RuntimeError, match="session is running"):
        await manager.stop()
    with pytest.raises(RuntimeError, match="session is running"):
        await manager.disable("alpha")
    assert manager.active_ids == ("alpha",)
    busy = False
    await manager.stop()


@pytest.mark.parametrize("phase", ["setup", "attach", "factory"])
async def test_partial_start_failure_reverse_cleanup_preserves_original_error(phase: str) -> None:
    registry = ExtensionRegistry()
    log: list[str] = []
    instances: list[Plugin] = []
    original = ValueError("original failure")
    handles: list[TaskHandle] = []
    finalized = asyncio.Event()

    async def background() -> None:
        try:
            await forever()
        finally:
            finalized.set()

    class FailingPlugin(Plugin):
        async def setup(self, api: SessionExtensionAPI) -> None:
            await super().setup(api)
            api.on("tick", lambda event: None)
            if phase == "setup" and self.name == "beta":
                raise original

        async def attach(self, ctx: SessionExtensionContext) -> None:
            await super().attach(ctx)
            handles.append(ctx.tasks.submit("background", background))
            await asyncio.sleep(0)
            if phase == "attach" and self.name == "beta":
                raise original

        async def detach(self) -> None:
            await super().detach()
            raise RuntimeError("detach failure must not replace original")

        async def dispose(self) -> None:
            await super().dispose()
            raise RuntimeError("dispose failure must not stop cleanup")

    def factory(name: str) -> Plugin:
        if phase == "factory" and name == "beta":
            raise original
        instance = FailingPlugin(name, log)
        instances.append(instance)
        return instance

    registry.register(manifest("alpha"), lambda: factory("alpha"))
    registry.register(manifest("beta"), lambda: factory("beta"))
    registry.register(manifest("gamma"), lambda: factory("gamma"))
    manager = SessionExtensionManager(
        registry,
        actions=Actions(),
        enabled=("alpha", "beta", "gamma"),
        grants=grants("alpha", "beta", "gamma"),
    )
    with pytest.raises(ValueError) as raised:
        await manager.start()
    assert raised.value is original
    if phase == "setup":
        assert log == ["alpha.setup", "beta.setup", "beta.dispose", "alpha.dispose"]
    elif phase == "factory":
        assert log == ["alpha.setup", "alpha.dispose"]
    else:
        assert log == [
            "alpha.setup",
            "beta.setup",
            "gamma.setup",
            "alpha.attach",
            "beta.attach",
            "gamma.dispose",
            "beta.detach",
            "beta.dispose",
            "alpha.detach",
            "alpha.dispose",
        ]
        assert finalized.is_set()
        for handle in handles:
            with pytest.raises(asyncio.CancelledError):
                await handle.wait()
    assert manager.active_ids == () and manager.tools() == ()
    for instance in instances:
        with pytest.raises(ExtensionRevokedError):
            instance.api.on("late", lambda event: None)
    await manager.stop()
    assert any(diagnostic.operation == "dispose" for diagnostic in manager.diagnostics)


@pytest.mark.parametrize("phase", ["setup", "attach"])
async def test_start_cancellation_preserves_cancel_and_cleans_attempted_activation(
    phase: str,
) -> None:
    entered = asyncio.Event()
    disposed = asyncio.Event()
    handles: list[TaskHandle] = []

    class BlockingPlugin(Plugin):
        async def setup(self, api: SessionExtensionAPI) -> None:
            await super().setup(api)
            api.on("tick", lambda event: None)
            if phase == "setup":
                entered.set()
                await forever()

        async def attach(self, ctx: SessionExtensionContext) -> None:
            await super().attach(ctx)
            handles.append(ctx.tasks.submit("owned", forever))
            entered.set()
            await forever()

        async def dispose(self) -> None:
            await super().dispose()
            disposed.set()
            raise RuntimeError("cleanup failure")

    plugin = BlockingPlugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    task = asyncio.create_task(manager.start())
    await entered.wait()
    task.cancel("original cancellation")
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert disposed.is_set() and manager.active_ids == ()
    assert ("alpha.detach" in plugin.log) is (phase == "attach")
    for handle in handles:
        with pytest.raises(asyncio.CancelledError):
            await handle.wait()
    with pytest.raises(ExtensionRevokedError):
        plugin.api.tasks.submit("late", forever)
    await manager.stop()


async def test_shutdown_waits_for_all_cleanup_despite_repeated_caller_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    plugin: Plugin

    class BlockingDetach(Plugin):
        async def detach(self) -> None:
            await super().detach()
            entered.set()
            await release.wait()

    plugin = BlockingDetach()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    handle = plugin.api.tasks.submit("owned", forever)
    task = asyncio.create_task(manager.stop())
    await entered.wait()
    assert manager.active_ids == ()
    with pytest.raises(ExtensionRevokedError):
        plugin.ctx.actions.enqueue(UserMessage("late", 1), "steer")
    task.cancel("one")
    await asyncio.sleep(0)
    task.cancel("two")
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin.log[-2:] == ["alpha.detach", "alpha.dispose"]
    with pytest.raises(asyncio.CancelledError):
        await handle.wait()
    await manager.stop()


async def test_lifecycle_timeout_disposes_and_detach_timeout_does_not_skip_dispose() -> None:
    class SlowPlugin(Plugin):
        async def attach(self, ctx: SessionExtensionContext) -> None:
            await super().attach(ctx)
            await forever()

        async def detach(self) -> None:
            await super().detach()
            await forever()

    plugin = SlowPlugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha"), timeout=0.01
    )
    with pytest.raises(TimeoutError):
        await manager.start()
    assert plugin.log == ["alpha.setup", "alpha.attach", "alpha.detach", "alpha.dispose"]
    assert [item.operation for item in manager.diagnostics] == ["attach", "detach"]


async def test_reentrant_mutation_from_plugin_is_rejected_without_deadlock() -> None:
    manager: SessionExtensionManager

    class ReentrantPlugin(Plugin):
        async def setup(self, api: SessionExtensionAPI) -> None:
            await super().setup(api)
            with pytest.raises(LifecycleError, match="Reentrant"):
                await manager.start()

        async def detach(self) -> None:
            await super().detach()
            with pytest.raises(LifecycleError, match="Reentrant"):
                await manager.stop()

    registry = ExtensionRegistry()
    registry.register(manifest(), ReentrantPlugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    await manager.stop()
    assert manager.diagnostics == ()


async def test_two_sessions_are_isolated_and_old_handles_do_not_keep_actions_alive() -> None:
    instances: list[Plugin] = []

    def factory() -> Plugin:
        plugin = Plugin()
        instances.append(plugin)
        return plugin

    registry = ExtensionRegistry()
    registry.register(manifest(), factory)
    first_actions = Actions("one")
    second_actions = Actions("two")
    first = SessionExtensionManager(
        registry, actions=first_actions, enabled=("alpha",), grants=grants("alpha")
    )
    second = SessionExtensionManager(
        registry, actions=second_actions, enabled=("alpha",), grants=grants("alpha")
    )
    await asyncio.gather(first.start(), second.start())
    one, two = instances
    assert (
        one is not two
        and one.ctx.binding.session_id == "one"
        and two.ctx.binding.session_id == "two"
    )
    one.ctx.actions.enqueue(UserMessage("one", 1), "steer")
    two.ctx.actions.enqueue(UserMessage("two", 1), "follow_up")
    assert [message.content for message in first_actions.queued] == ["one"]
    assert [message.content for message in second_actions.queued] == ["two"]
    task_one = one.api.tasks.submit("work", forever)
    task_two = two.api.tasks.submit("work", forever)
    assert task_one.id != task_two.id
    await first.stop()
    with pytest.raises(asyncio.CancelledError):
        await task_one.wait()
    assert not two.api.tasks.list()[0].done
    reference = weakref.ref(first_actions)
    del first, first_actions
    gc.collect()
    assert reference() is None  # Retained one.api/one.ctx/TaskHandle do not hold host actions.
    await second.stop()
    with pytest.raises(asyncio.CancelledError):
        await task_two.wait()


async def test_tasks_observe_errors_explicit_timeouts_cancel_wait_list_and_self_cancel_all() -> (
    None
):
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    service = plugin.api.tasks
    error = RuntimeError("background failure")

    async def broken() -> None:
        raise error

    failed = service.submit("broken", broken)
    with pytest.raises(RuntimeError) as raised:
        await failed.wait()
    assert raised.value is error
    timeout = service.submit("timeout", forever, timeout=0.01)
    with pytest.raises(TimeoutError):
        await timeout.wait()
    assert [item.operation for item in manager.diagnostics] == ["task:broken", "task:timeout"]
    assert all(info.done for info in service.list())
    handles = [service.submit("waiting", forever) for _ in range(2)]

    async def self_cancel() -> None:
        await service.cancel_all()  # Cancels other tasks, not itself, and cannot self-wait.

    itself = service.submit("self", self_cancel)
    await itself.wait()
    for handle in handles:
        with pytest.raises(asyncio.CancelledError):
            await handle.wait()
    direct = service.submit("direct", forever)
    direct.cancel()
    with pytest.raises(asyncio.CancelledError):
        await direct.wait()
    assert all(info.done for info in service.list())
    await manager.stop()
    with pytest.raises(ExtensionRevokedError):
        service.submit("revoked", no_work)
    await service.cancel_all()


async def test_tasks_are_bounded_and_submit_rejected_during_cancel_and_lifecycle_from_work() -> (
    None
):
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    service = plugin.api.tasks
    rejected = asyncio.Event()
    entered = asyncio.Event()

    async def cleanup_tries_to_spawn() -> None:
        entered.set()
        try:
            await forever()
        finally:
            with pytest.raises(LifecycleError):
                service.submit("cannot escape cancellation", forever)
            rejected.set()

    service.submit("cleanup", cleanup_tries_to_spawn)
    await entered.wait()
    for _ in range(127):
        service.submit("bounded", forever)
    with pytest.raises(LifecycleError, match="limit"):
        service.submit("overflow", forever)
    await service.cancel_all()
    assert rejected.is_set() and all(info.done for info in service.list())

    async def cannot_stop_own_manager() -> None:
        with pytest.raises(LifecycleError, match="owned task"):
            await manager.stop()

    handle = service.submit("lifecycle", cannot_stop_own_manager)
    await handle.wait()
    await manager.stop()


async def test_waiter_cancellation_does_not_lose_owned_task() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    handle = plugin.api.tasks.submit("still owned", forever)
    waiter = asyncio.create_task(handle.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not plugin.api.tasks.list()[0].done
    await manager.stop()
    with pytest.raises(asyncio.CancelledError):
        await handle.wait()


async def test_cancel_all_reentrant_finalizers_cannot_wait_for_their_parent() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    service = plugin.api.tasks
    entered = asyncio.Event()
    finalized: list[str] = []

    async def child() -> None:
        entered.set()
        try:
            await forever()
        finally:
            await service.cancel_all()
            finalized.append("child")

    service.submit("child", child)
    await entered.wait()

    async def parent() -> None:
        await service.cancel_all()
        finalized.append("parent")

    handle = service.submit("parent", parent)
    async with asyncio.timeout(1):
        await handle.wait()
    assert finalized == ["child", "parent"]
    await manager.stop()


async def test_task_self_wait_and_timeout_suppression_cannot_escape_ownership() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    service = plugin.api.tasks
    handle: TaskHandle

    async def self_wait() -> None:
        with pytest.raises(LifecycleError, match="itself"):
            await handle.wait()

    handle = service.submit("self-wait", self_wait)
    await handle.wait()

    async def suppress_timeout() -> None:
        try:
            await forever()
        except asyncio.CancelledError:
            pass

    timed = service.submit("timeout", suppress_timeout, timeout=0.01)
    with pytest.raises(TimeoutError):
        await timed.wait()
    with pytest.raises(ValueError):
        service.submit("", no_work)
    with pytest.raises(ValueError):
        service.submit("invalid timeout", no_work, timeout=0)
    await manager.stop()


async def test_disable_cancellation_completes_unload_and_keeps_other_extensions_active() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingDetach(Plugin):
        async def detach(self) -> None:
            entered.set()
            await release.wait()
            await super().detach()

    first = BlockingDetach()
    second = Plugin("beta")
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: first)
    registry.register(manifest("beta"), lambda: second)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha", "beta"), grants=grants("alpha", "beta")
    )
    await manager.start()
    task = asyncio.create_task(manager.disable("alpha"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.active_ids == ("beta",)
    second.api.enqueue(UserMessage("still active", 1), "steer")
    with pytest.raises(ExtensionRevokedError):
        first.api.enqueue(UserMessage("disabled", 1), "steer")
    await manager.stop()


async def test_disable_can_remove_invalid_stopped_configuration_without_starting_it() -> None:
    registry = ExtensionRegistry()
    registry.register(manifest("alpha", ("missing",)), Plugin)
    manager = SessionExtensionManager(registry, actions=Actions(), enabled=("alpha",))
    await manager.disable("alpha")
    await manager.start()
    assert manager.active_ids == ()
    await manager.stop()


async def test_cleanup_diagnostics_cannot_replace_original_unprintable_exception() -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise ValueError("broken exception rendering")

    original = UnprintableError()

    class Broken(Plugin):
        async def attach(self, ctx: SessionExtensionContext) -> None:
            await super().attach(ctx)
            raise original

        async def detach(self) -> None:
            await super().detach()
            raise UnprintableError()

    plugin = Broken()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    with pytest.raises(UnprintableError) as raised:
        await manager.start()
    assert raised.value is original
    assert plugin.log[-2:] == ["alpha.detach", "alpha.dispose"]
    assert len(manager.diagnostics) == 2
    assert "unprintable" in manager.diagnostics[0].message


async def test_unawaited_background_failure_is_observed_and_diagnosed() -> None:
    plugin = Plugin()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unexpected: list[str] = []
    loop.set_exception_handler(lambda loop, context: unexpected.append(str(context["message"])))

    async def broken() -> None:
        raise ValueError("nobody awaits this task")

    try:
        plugin.api.tasks.submit("unawaited", broken)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        assert unexpected == []
        assert manager.diagnostics[-1].operation == "task:unawaited"
    finally:
        loop.set_exception_handler(previous)
        await manager.stop()


async def test_eager_task_factory_cannot_bypass_task_admission_or_cleanup_reentrancy() -> None:
    manager: SessionExtensionManager

    class ReentrantDetach(Plugin):
        async def detach(self) -> None:
            await super().detach()
            with pytest.raises(LifecycleError, match="Reentrant"):
                await manager.stop()

    plugin = ReentrantDetach()
    registry = ExtensionRegistry()
    registry.register(manifest(), lambda: plugin)
    manager = SessionExtensionManager(
        registry, actions=Actions(), enabled=("alpha",), grants=grants("alpha")
    )
    await manager.start()
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        handle: TaskHandle

        async def work() -> None:
            assert any(info.id == handle.id for info in plugin.api.tasks.list())
            with pytest.raises(LifecycleError, match="owned task"):
                await manager.stop()

        handle = plugin.api.tasks.submit("eager", work)
        await handle.wait()
        await manager.stop()
        assert manager.diagnostics == ()
    finally:
        loop.set_task_factory(previous)
        await manager.stop()
