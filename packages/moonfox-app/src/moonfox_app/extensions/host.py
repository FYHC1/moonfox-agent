"""Host extension lifecycle and a revocable adapter over an injected channel port."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from typing import cast

from moonfox_app.extension_api import (
    ChannelControl,
    ChannelGateway,
    ChannelInput,
    HostExtension,
    InputReceipt,
    RegistrationHandle,
    ReplyEnvelope,
    SessionExtension,
    TaskService,
)

from .dispatch import cancellation_pending, invoke
from .errors import CapabilityError, RegistrationError
from .lifecycle import Instance, LifecycleManager, Owner
from .registry import ExtensionRegistry, RegisteredExtension


class ScopedChannel:
    def __init__(self, owner: Owner, gateway: ChannelGateway, timeout: float) -> None:
        self._owner = owner
        self._gateway: ChannelGateway | None = gateway
        self._timeout = timeout
        owner.track(self._release)

    def _release(self) -> None:
        self._gateway = None

    def _port(self, *, active: bool = True) -> ChannelGateway:
        self._owner.require("channel", active=active)
        assert self._gateway is not None
        return self._gateway

    async def submit(self, message: ChannelInput) -> InputReceipt:
        port = self._port()
        if (
            not isinstance(message, ChannelInput)
            or type(message.received_at) is not int
            or not all(
                isinstance(value, str)
                for value in (
                    message.connection_id,
                    message.bot_account,
                    message.target,
                    message.speaker,
                    message.external_message_id,
                    message.content,
                )
            )
        ):
            raise RegistrationError("Channel submit requires a ChannelInput")
        result = await invoke(lambda: port.submit(deepcopy(message)), self._timeout)
        self._owner.require("channel", active=True)
        if not isinstance(result, InputReceipt):
            raise RegistrationError("Channel gateway returned an invalid InputReceipt")
        return deepcopy(result)

    async def control(self, command: ChannelControl) -> None:
        port = self._port()
        if (
            not isinstance(command, ChannelControl)
            or command.action != "cancel"
            or not isinstance(command.connection_id, str)
            or not isinstance(command.target, str)
        ):
            raise RegistrationError("Channel control requires a cancel ChannelControl")
        await invoke(lambda: port.control(deepcopy(command)), self._timeout)

    def subscribe_replies(
        self, listener: Callable[[ReplyEnvelope], Awaitable[None]]
    ) -> RegistrationHandle:
        port = self._port(active=False)  # Subscribing declares registration, not channel input.
        if not callable(listener):
            raise RegistrationError("Reply listener must be callable")
        callback: Callable[[ReplyEnvelope], Awaitable[None]] | None = listener

        async def receive(reply: ReplyEnvelope) -> None:
            selected = callback
            if selected is None or self._owner.phase != "active":
                return
            try:
                if not isinstance(reply, ReplyEnvelope):
                    raise RegistrationError("Channel reply must be ReplyEnvelope")
                await invoke(lambda: selected(deepcopy(reply)), self._timeout)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                self._owner.failure("channel.reply", exc)

        upstream = port.subscribe_replies(receive)

        def release() -> None:
            nonlocal callback
            callback = None
            upstream.dispose()

        return self._owner.track(release)


class HostAPI:
    def __init__(self, owner: Owner, channel: ChannelGateway | None, timeout: float) -> None:
        self._owner = owner
        self._channel = ScopedChannel(owner, channel, timeout) if channel is not None else None

    @property
    def channel(self) -> ChannelGateway:
        self._owner.require("channel")
        if self._channel is None:
            raise CapabilityError("No channel gateway was injected")
        return self._channel

    @property
    def tasks(self) -> TaskService:
        self._owner.require("tasks")
        return self._owner.tasks


class HostExtensionManager(LifecycleManager):
    """One host instance per explicitly enabled factory; no network implementation."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        channel: ChannelGateway | None = None,
        enabled: Sequence[str] = (),
        grants: Mapping[str, frozenset[str]] | None = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(registry, scope="host", enabled=enabled, grants=grants, timeout=timeout)
        self._channel = channel

    def _preflight(self) -> tuple[RegisteredExtension, ...]:
        entries = super()._preflight()
        if self._channel is None and any(
            "channel" in item.manifest.capabilities for item in entries
        ):
            raise CapabilityError("Enabled host extensions require an injected channel gateway")
        return entries

    def _bind(self, owner: Owner, plugin: SessionExtension | HostExtension) -> Instance:
        host = cast(HostExtension, plugin)
        api = HostAPI(owner, self._channel, self._timeout)
        return Instance(
            owner,
            lambda: host.setup(api),
            lambda: host.start(),
            lambda: host.stop(),
            lambda: host.dispose(),
        )
