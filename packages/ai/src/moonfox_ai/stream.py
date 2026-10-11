"""Single-consumer bounded streams with a separately awaitable final result."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, cast

from .types import AssistantMessage, AssistantMessageEvent, Done, Error, Start, now_ms

_END = object()
_MISSING = object()


class StreamProtocolError(RuntimeError):
    """The producer violated the stream contract (a programming error)."""


class IncompleteStreamError(StreamProtocolError):
    """The producer ended without a terminal event or an explicit result."""


class StreamOverflowError(RuntimeError):
    """Events were lost; discard the partial view and obtain the final result."""


class StreamClosedError(RuntimeError):
    """The consumer closed an unfinished stream."""


class EventStream[T, R]:
    """One producer and ONE event iterator, not a broadcast abstraction.

    result() supports multiple independent waiters and never consumes events.
    Terminal events finish both interfaces without a separate end() call. An
    unfinished end() fails deterministically. Canceling a result waiter does not
    cancel the producer. The bounded event buffer never blocks result-only users;
    overflow explicitly fails iteration rather than silently dropping deltas.
    """

    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
        *,
        max_buffer: int = 256,
    ) -> None:
        if isinstance(max_buffer, bool) or not isinstance(max_buffer, int) or max_buffer < 1:
            raise ValueError("max_buffer must be a positive integer")
        self._queue: asyncio.Queue[T | object] = asyncio.Queue(maxsize=max_buffer)
        self._settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._value: R | object = _MISSING
        self._failure: Exception | None = None
        self._overflow = False
        self._done = False
        self._claimed = False
        self._producer: asyncio.Task[None] | None = None

    @property
    def done(self) -> bool:
        return self._done

    def push(self, event: T) -> None:
        if self._done:
            return
        complete = self._is_complete(event)
        if complete:
            self._value = self._extract_result(event)
        if not self._overflow:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                self._overflow = True
                while not self._queue.empty():
                    self._queue.get_nowait()
                self._queue.put_nowait(_END)
        if complete:
            self._finish()

    def _finish(self) -> None:
        self._done = True
        self._settled.set_result(None)
        if self._queue.empty():
            self._queue.put_nowait(_END)

    def end(self, result: R | object = _MISSING) -> None:
        if self._done:
            return
        if result is _MISSING:
            self.fail(IncompleteStreamError("Stream ended without a terminal result"))
        else:
            self._value = result
            self._finish()

    def fail(self, error: Exception) -> None:
        """Propagate producer bugs to callers; operational failures belong in events."""
        if not self._done:
            self._failure = error
            self._finish()

    def start(self, producer: Coroutine[Any, Any, None]) -> None:
        """Own the producer task so aclose() can cancel and join it."""
        if self._producer is not None or self._done:
            producer.close()
            raise StreamProtocolError("A stream can own only one producer")
        self._producer = asyncio.create_task(self._run_producer(producer))

    async def _run_producer(self, producer: Coroutine[Any, Any, None]) -> None:
        try:
            await producer
        except asyncio.CancelledError:
            self.fail(StreamClosedError("Stream producer was cancelled"))
        except Exception as exc:
            self.fail(exc)
        finally:
            if not self._done:
                self.end()

    def __aiter__(self) -> AsyncIterator[T]:
        if self._claimed:
            raise StreamProtocolError("EventStream permits only one event iterator")
        self._claimed = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        while True:
            if self._overflow:
                raise StreamOverflowError("Event buffer overflow; use result() to resynchronize")
            if self._queue.empty() and self._done:
                if self._failure is not None:
                    raise self._failure
                return
            event = await self._queue.get()
            if event is _END:
                if self._overflow:
                    raise StreamOverflowError(
                        "Event buffer overflow; use result() to resynchronize"
                    )
                if self._failure is not None:
                    raise self._failure
                return
            yield cast(T, event)

    async def result(self) -> R:
        await asyncio.shield(self._settled)
        if self._failure is not None:
            raise self._failure
        return cast(R, self._value)

    async def aclose(self) -> None:
        if self._producer is not None:
            if not self._producer.done():
                # Let the owned coroutine enter its try/finally before cancellation.
                await asyncio.sleep(0)
                self._producer.cancel()
            await asyncio.gather(self._producer, return_exceptions=True)
        if not self._done:
            self.fail(StreamClosedError("Stream closed before completion"))


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """Start -> content events -> Done; failures may emit Error without Start.

    Partial objects remain live. Consumers retaining snapshots must deepcopy.
    Timing is recorded only for messages originating in this stream.
    """

    def __init__(self, *, max_buffer: int = 256) -> None:
        super().__init__(
            is_complete=lambda event: isinstance(event, (Done, Error)),
            extract_result=self._extract,
            max_buffer=max_buffer,
        )
        self._started = False
        self._created_at = now_ms()
        self._monotonic = time.perf_counter()

    @staticmethod
    def _extract(event: AssistantMessageEvent) -> AssistantMessage:
        if isinstance(event, Done):
            return event.message
        if isinstance(event, Error):
            return event.error
        raise StreamProtocolError("Cannot extract a result from a nonterminal event")

    def push(self, event: AssistantMessageEvent) -> None:
        if self.done:
            return
        if isinstance(event, Start):
            if self._started:
                raise StreamProtocolError("Duplicate Start event")
            self._started = True
        elif not self._started and not isinstance(event, Error):
            raise StreamProtocolError("Content events and Done require a preceding Start")
        if isinstance(event, (Done, Error)):
            message = self._extract(event)
            if message.stop_reason != event.reason:
                raise StreamProtocolError("Terminal reason does not match the message")
            if message.duration_ms is None and message.timestamp >= self._created_at:
                message.duration_ms = max(0, round((time.perf_counter() - self._monotonic) * 1000))
        super().push(event)
