"""Per-session replay windows and independent bounded subscriptions, not core stream queues."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from .errors import ResyncRequired

_CLOSED = object()


class Subscription:
    def __init__(self, capacity: int, remove: Callable[[], None]) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(capacity)
        self._remove = remove
        self._closed = False
        self._overflow = False

    def push(self, event: dict[str, Any]) -> None:
        if self._closed or self._overflow:
            return
        try:
            self._queue.put_nowait(deepcopy(event))
        except asyncio.QueueFull:
            self._overflow = True
            self._drain()
            self._queue.put_nowait(_CLOSED)
            self._remove()

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def next(self) -> dict[str, Any]:
        if self._overflow:
            raise ResyncRequired("Subscriber buffer overflow")
        if self._closed:
            raise ResyncRequired("Subscription closed")
        item = await self._queue.get()
        if not isinstance(item, dict):
            raise ResyncRequired("Subscription requires resynchronization")
        return deepcopy(item)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove()
        self._drain()
        self._queue.put_nowait(_CLOSED)


class EventBus:
    """Single-event-loop mutation; subscribe/publish never await."""

    def __init__(self, *, window: int, capacity: int) -> None:
        if window < 1 or capacity < 1:
            raise ValueError("Event window and capacity must be positive")
        self.epoch = uuid4().hex
        self._window = window
        self._capacity = capacity
        self._seq: dict[str, int] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._subscribers: dict[str, dict[str, Subscription]] = {}
        self._closed = False

    def watermark(self, session_id: str) -> tuple[str, int]:
        return self.epoch, self._seq.get(session_id, 0)

    def publish(self, session_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Event bus is closed")
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = {
            "protocol_version": 1,
            "epoch": self.epoch,
            "session_id": session_id,
            "seq": seq,
            "type": event_type,
            "payload": deepcopy(payload),
        }
        self._history.setdefault(session_id, deque(maxlen=self._window)).append(event)
        for subscription in tuple(self._subscribers.get(session_id, {}).values()):
            subscription.push(event)
        return deepcopy(event)

    def subscribe(self, session_id: str, *, epoch: str, after_seq: int) -> Subscription:
        if self._closed or epoch != self.epoch or type(after_seq) is not int or after_seq < 0:
            raise ResyncRequired("Invalid event epoch or cursor")
        current = self._seq.get(session_id, 0)
        history = self._history.get(session_id, deque())
        earliest = history[0]["seq"] if history else current + 1
        if after_seq > current or after_seq < earliest - 1:
            raise ResyncRequired("Event cursor outside replay window")
        replay = [event for event in history if event["seq"] > after_seq]
        if len(replay) > self._capacity:
            raise ResyncRequired("Replay exceeds subscriber buffer")
        key = uuid4().hex
        listeners = self._subscribers.setdefault(session_id, {})

        def remove() -> None:
            listeners.pop(key, None)

        subscription = Subscription(self._capacity, remove)
        listeners[key] = subscription
        for event in replay:
            subscription.push(event)
        return subscription

    def close(self) -> None:
        self._closed = True
        for listeners in tuple(self._subscribers.values()):
            for subscription in tuple(listeners.values()):
                subscription.close()
        self._subscribers.clear()
        self._history.clear()
