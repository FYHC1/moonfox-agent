"""Local notifications: snapshot order, detached payloads, isolated bounded handlers."""

import asyncio
import inspect
from collections.abc import Callable
from copy import deepcopy

from moonfox_app.extension_api import EventHandler, ExtensionDiagnostic, SessionEvent


class ListenerHandle:
    def __init__(self, remove: Callable[[], None]) -> None:
        self._remove = remove

    def dispose(self) -> None:
        self._remove()


class SessionNotifications:
    def __init__(self, *, timeout: float) -> None:
        if timeout <= 0:
            raise ValueError("Notification timeout must be positive")
        self._timeout = timeout
        self._listeners: dict[int, EventHandler] = {}
        self._next = 0
        self.diagnostics: list[ExtensionDiagnostic] = []

    def subscribe(self, listener: EventHandler) -> ListenerHandle:
        key = self._next
        self._next += 1
        self._listeners[key] = listener

        def remove() -> None:
            self._listeners.pop(key, None)

        return ListenerHandle(remove)

    async def emit(self, event: SessionEvent) -> None:
        for callback in tuple(self._listeners.values()):
            try:
                async with asyncio.timeout(self._timeout):
                    result = callback(deepcopy(event))
                    if inspect.isawaitable(result):
                        await result
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                self.diagnostics.append(ExtensionDiagnostic("<listener>", event.type, "cancelled"))
            except Exception as exc:
                self.diagnostics.append(ExtensionDiagnostic("<listener>", event.type, str(exc)))

    def clear(self) -> None:
        self._listeners.clear()
