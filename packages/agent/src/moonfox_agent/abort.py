"""Cooperative, one-way cancellation; each run owns a fresh token."""

import asyncio

from moonfox_ai.api import AbortSignal


class AbortToken:
    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def abort(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class CombinedAbortSignal:
    """Read-only union of cancellation sources; waiting owns and joins its helpers."""

    def __init__(self, *signals: AbortSignal | None) -> None:
        self._signals = tuple(signal for signal in signals if signal is not None)

    @property
    def aborted(self) -> bool:
        return any(signal.aborted for signal in self._signals)

    async def wait(self) -> None:
        if self.aborted:
            return
        tasks = [asyncio.create_task(signal.wait()) for signal in self._signals]
        if not tasks:
            await asyncio.Event().wait()
            return
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
