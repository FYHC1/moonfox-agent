"""Owner-scoped, observable asyncio work. Nothing is scheduled during setup."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from uuid import uuid4

from moonfox_app.extension_api import ExtensionDiagnostic, TaskInfo

from .dispatch import validate_timeout
from .errors import ExtensionRevokedError, LifecycleError, describe


class OwnedTaskHandle:
    def __init__(self, task_id: str, task: asyncio.Task[None]) -> None:
        self._id = task_id
        self._task = task

    @property
    def id(self) -> str:
        return self._id

    def cancel(self) -> None:
        self._task.cancel()

    async def wait(self) -> None:
        if asyncio.current_task() is self._task:
            raise LifecycleError("An owned task cannot wait for itself")
        # Cancelling a waiter must not silently abandon/cancel its background work.
        await asyncio.shield(self._task)


class OwnedTaskService:
    """At most 128 live tasks and 128 completed metadata records per owner.

    cancel_all excludes its calling task to prevent self-wait; revocation closes
    admission before cancelling. Completed handles remain waitable, but the host
    does not retain completed task objects or their exception tracebacks.
    """

    def __init__(
        self,
        owner: str,
        guard: Callable[[], None],
        diagnose: Callable[[ExtensionDiagnostic], None],
    ) -> None:
        self._owner = owner
        self._guard = guard
        self._diagnose = diagnose
        self._prefix = f"{owner}:{uuid4().hex}"
        self._next = 0
        self._live: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._completed: deque[TaskInfo] = deque(maxlen=128)
        self._closed = False
        self._draining = 0

    def submit(
        self, name: str, work: Callable[[], Awaitable[None]], *, timeout: float | None = None
    ) -> OwnedTaskHandle:
        if self._closed:
            raise ExtensionRevokedError(f"Task service for {self._owner} was revoked")
        self._guard()
        if self._draining:
            raise LifecycleError("Cannot submit while cancelling owned tasks")
        if not isinstance(name, str) or not name.strip() or not callable(work):
            raise ValueError("Task name must be nonempty and work must be an awaitable factory")
        if timeout is not None:
            validate_timeout(timeout)
        if len(self._live) >= 128:
            raise LifecycleError("Owner task limit reached (128)")
        self._next += 1
        task_id = f"{self._prefix}:{self._next}"

        admitted = asyncio.Event()

        async def run() -> None:
            # Eager task factories must not run plugin code before ownership is recorded.
            await admitted.wait()
            try:
                async with asyncio.timeout(timeout) as limit:
                    await work()
                if limit.expired():
                    raise TimeoutError("Owned task timed out")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._diagnose(ExtensionDiagnostic(self._owner, f"task:{name}", describe(exc)))
                raise

        coroutine = run()
        try:
            task = asyncio.create_task(coroutine, name=task_id)
        except BaseException:
            coroutine.close()
            raise
        self._live[task_id] = (name, task)

        def completed(done: asyncio.Task[None]) -> None:
            self._live.pop(task_id, None)
            self._completed.append(TaskInfo(task_id, name, True, done.cancelled()))
            if not done.cancelled():
                done.exception()  # Observe even when no extension ever awaits its handle.

        task.add_done_callback(completed)
        admitted.set()
        return OwnedTaskHandle(task_id, task)

    def list(self) -> tuple[TaskInfo, ...]:
        return tuple(self._completed) + tuple(
            TaskInfo(task_id, name, task.done(), task.cancelled())
            for task_id, (name, task) in self._live.items()
        )

    def owns_current_task(self) -> bool:
        current = asyncio.current_task()
        return any(task is current for _, task in self._live.values())

    def close(self) -> None:
        self._closed = True

    async def cancel_all(self) -> None:
        current = asyncio.current_task()
        if self._draining and self.owns_current_task():
            # A task being cancelled may call cancel_all from its finally block.
            # Waiting for a sibling/parent already draining it would form a cycle.
            return
        tasks = tuple(task for _, task in self._live.values() if task is not current)
        self._draining += 1
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._draining -= 1
