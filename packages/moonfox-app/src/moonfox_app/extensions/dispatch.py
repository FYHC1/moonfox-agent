"""Four small, snapshot-based dispatch policies, independent of lifecycle and runtime."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import cast

from moonfox_app.extension_api import ExtensionDiagnostic, PermissionDecision

from .errors import RegistrationError, describe


def validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be finite and positive")


def cancellation_pending() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def invoke(work: Callable[[], object], seconds: float) -> object:
    """Bound cooperative callbacks; suppressing cancellation cannot turn failure into success."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    task = asyncio.current_task()
    cancellations = 0 if task is None else task.cancelling()
    async with asyncio.timeout_at(deadline) as limit:
        result = work()
        if inspect.isawaitable(result):
            result = await cast(Awaitable[object], result)
    if limit.expired() or loop.time() >= deadline:
        raise TimeoutError("Extension callback timed out")
    if task is not None and task.cancelling() > cancellations:
        raise asyncio.CancelledError
    return result


def validate_decision(value: object) -> PermissionDecision:
    if (
        not isinstance(value, PermissionDecision)
        or type(value.allowed) is not bool
        or not isinstance(value.reason, str)
    ):
        raise RegistrationError("Checker must return PermissionDecision(bool, str)")
    return value


@dataclass(frozen=True, slots=True)
class Handler[T, R]:
    extension_id: str
    callback: Callable[[T], R | Awaitable[R]]


class Dispatcher:
    """Validators raise on invalid results; no failed intermediate state escapes.

    A callback-raised CancelledError is a callback failure. Cancellation requested
    by the caller is propagated, never translated to success or swallowed.
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        diagnose: Callable[[ExtensionDiagnostic], None] | None = None,
    ) -> None:
        validate_timeout(timeout)
        self.timeout = timeout
        self._diagnose = diagnose

    def failure(self, owner: str, operation: str, exc: BaseException) -> None:
        if self._diagnose is not None:
            self._diagnose(ExtensionDiagnostic(owner, operation, describe(exc)))

    async def notify[T, R](
        self,
        handlers: Sequence[Handler[T, R]],
        value: T,
        *,
        operation: str = "notify",
    ) -> None:
        snapshot = deepcopy(value)
        for handler in tuple(handlers):
            try:
                await invoke(partial(handler.callback, deepcopy(snapshot)), self.timeout)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                self.failure(handler.extension_id, operation, exc)

    async def chain[T](
        self,
        handlers: Sequence[Handler[T, T]],
        value: T,
        validate: Callable[[T], None],
    ) -> T:
        current = deepcopy(value)
        validate(deepcopy(current))
        for handler in tuple(handlers):
            try:
                candidate = cast(
                    T, await invoke(partial(handler.callback, deepcopy(current)), self.timeout)
                )
                validate(deepcopy(candidate))
                current = deepcopy(candidate)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                self.failure(handler.extension_id, "chain", exc)
        return current

    async def mutate[T](
        self,
        handlers: Sequence[Handler[T, None]],
        value: T,
        validate: Callable[[T], None],
    ) -> T:
        current = deepcopy(value)
        validate(deepcopy(current))
        for handler in tuple(handlers):
            try:
                candidate = deepcopy(current)
                result = await invoke(partial(handler.callback, candidate), self.timeout)
                if result is not None:
                    raise RegistrationError("In-place handlers must return None")
                validate(deepcopy(candidate))
                current = deepcopy(candidate)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                self.failure(handler.extension_id, "mutate", exc)
        return current

    async def gate[T](
        self,
        handlers: Sequence[Handler[T, PermissionDecision]],
        value: T,
        *,
        operation: str = "gate",
    ) -> PermissionDecision:
        snapshot = deepcopy(value)
        for handler in tuple(handlers):
            try:
                result = await invoke(partial(handler.callback, deepcopy(snapshot)), self.timeout)
                decision = validate_decision(result)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError) and cancellation_pending():
                    raise
                self.failure(handler.extension_id, operation, exc)
                return PermissionDecision(False, f"{handler.extension_id}: {operation} failed")
            if not decision.allowed:
                return deepcopy(decision)
        return PermissionDecision(True)
