"""Bound channel capability and conservative outbox delivery; no platform protocol code."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from moonfox_app.extension_api import (
    ChannelControl,
    ChannelInput,
    InputReceipt,
    RegistrationHandle,
    ReplyEnvelope,
)
from moonfox_app.security import AccessDenied

from .store import HostStore
from .types import SessionRecord


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    connection_id: str
    bot_account: str
    owner_id: str
    targets: frozenset[str]

    def __post_init__(self) -> None:
        if not self.connection_id or not self.bot_account or not self.owner_id or not self.targets:
            raise ValueError("Channel binding needs explicit connection/account/owner/targets")


class ChannelRouter(Protocol):
    def channel_session(self, binding: ChannelBinding, target: str) -> SessionRecord: ...

    def channel_input(self, session: SessionRecord, *, text: str, key: str) -> InputReceipt: ...

    async def channel_cancel(self, binding: ChannelBinding, target: str) -> None: ...


class _Handle:
    def __init__(self, remove: Callable[[], None]) -> None:
        self._remove = remove

    def dispose(self) -> None:
        self._remove()


class BoundChannelGateway:
    """Created by trusted bootstrap after connection authorization, never by a model/HTTP DTO."""

    def __init__(
        self,
        router: ChannelRouter,
        store: HostStore,
        binding: ChannelBinding,
        *,
        delivery_timeout: float = 30.0,
    ) -> None:
        self._router = router
        self._store = store
        self._binding = binding
        self._timeout = delivery_timeout
        self._listener: Callable[[ReplyEnvelope], Awaitable[None]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._sessions: dict[str, str] = {}
        self._dirty = False

    def _check(self, connection_id: str, target: str) -> None:
        if (
            self._closed
            or connection_id != self._binding.connection_id
            or target not in self._binding.targets
        ):
            raise AccessDenied("Channel capability does not authorize this connection/target")

    async def submit(self, message: ChannelInput) -> InputReceipt:
        self._check(message.connection_id, message.target)
        if message.bot_account != self._binding.bot_account:
            raise AccessDenied("Channel bot account mismatch")
        if not message.speaker or not message.external_message_id:
            raise ValueError("Channel message needs speaker and external message ID")
        session = self._router.channel_session(self._binding, message.target)
        self._sessions[session.id] = message.target
        key = hashlib.sha256(
            json.dumps([message.bot_account, message.target, message.external_message_id]).encode()
        ).hexdigest()
        return self._router.channel_input(session, text=message.content, key=key)

    async def control(self, command: ChannelControl) -> None:
        self._check(command.connection_id, command.target)
        if command.action != "cancel":
            raise ValueError("Unsupported channel control")
        await self._router.channel_cancel(self._binding, command.target)

    def subscribe_replies(
        self, listener: Callable[[ReplyEnvelope], Awaitable[None]]
    ) -> RegistrationHandle:
        if self._closed or self._listener is not None:
            raise RuntimeError("Channel already has a reply transport or is closed")
        # Publish the registration only after routing succeeds, so failure is retryable.
        sessions = dict(self._sessions)
        for target in self._binding.targets:
            record = self._router.channel_session(self._binding, target)
            sessions[record.id] = target
        self._sessions = sessions
        self._listener = listener
        self.wake()

        def remove() -> None:
            if self._listener is listener:
                self._listener = None
                if self._task is not None and not self._task.done():
                    self._task.cancel()

        return _Handle(remove)

    def wake(self) -> None:
        if self._closed or self._listener is None:
            return
        self._dirty = True
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._pump(), name="moonfox-channel-outbox")

        def settled(task: asyncio.Task[None]) -> None:
            if not task.cancelled():
                task.exception()
            if self._dirty:
                self.wake()

        self._task.add_done_callback(settled)

    async def _pump(self) -> None:
        while self._dirty and self._listener is not None and not self._closed:
            self._dirty = False
            await self._pump_once()

    async def _pump_once(self) -> None:
        for session_id, target in tuple(self._sessions.items()):
            for record in self._store.outbox(session_id):
                listener = self._listener
                if self._closed or listener is None:
                    return
                if record["status"] != "pending":
                    continue
                self._store.set_delivery(record["id"], expected="pending", status="sending")
                state = "unknown"
                try:
                    async with asyncio.timeout(self._timeout):
                        await listener(
                            ReplyEnvelope(record["run_id"], record["id"], target, record["text"])
                        )
                    state = "sent"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transport exception cannot prove the remote side did not send.
                    state = "unknown"
                finally:
                    self._store.set_delivery(record["id"], expected="sending", status=state)

    async def aclose(self) -> None:
        self._closed = True
        self._listener = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
