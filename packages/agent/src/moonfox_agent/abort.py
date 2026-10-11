"""Cooperative, one-way cancellation; each run owns a fresh token."""

import asyncio


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
