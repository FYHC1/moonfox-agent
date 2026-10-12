"""Single-use tool approvals bound to a session/input, cancelled and expired fail-closed."""

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from moonfox_ai.api import now_ms
from moonfox_app.extension_api import PermissionDecision, ToolPermissionRequest


class ApprovalStore(Protocol):
    def create_approval(
        self,
        *,
        session_id: str,
        input_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        expires_at: int,
    ) -> dict[str, Any]: ...

    def approval(self, identity: str) -> dict[str, Any]: ...

    def decide_approval(
        self, identity: str, *, status: str, principal_id: str | None = None
    ) -> dict[str, Any]: ...


class Approvals:
    def __init__(
        self,
        store: ApprovalStore,
        publish: Callable[[str, str, dict[str, Any]], object],
        *,
        timeout: float,
    ) -> None:
        self._store = store
        self._publish = publish
        self._timeout = timeout
        self._waiters: dict[str, tuple[str, asyncio.Future[bool]]] = {}

    async def request(self, request: ToolPermissionRequest, input_id: str) -> PermissionDecision:
        entry = self._store.create_approval(
            session_id=request.binding.session_id,
            input_id=input_id,
            tool_name=request.tool_name,
            arguments=request.arguments,
            expires_at=now_ms() + int(self._timeout * 1000),
        )
        future = asyncio.get_running_loop().create_future()
        self._waiters[entry["id"]] = (request.binding.session_id, future)
        self._publish(request.binding.session_id, "approval_requested", {"approval": entry})
        try:
            async with asyncio.timeout(self._timeout):
                allowed = await future
            return PermissionDecision(allowed, "Approved once" if allowed else "Approval denied")
        except TimeoutError:
            return PermissionDecision(False, "Approval expired")
        finally:
            self._waiters.pop(entry["id"], None)
            current = self._store.approval(entry["id"])
            if current["status"] == "pending":
                current = self._store.decide_approval(entry["id"], status="expired")
            self._publish(request.binding.session_id, "approval_resolved", {"approval": current})

    def resolve(self, identity: str, *, decision: str, principal_id: str) -> dict[str, Any]:
        if decision not in ("approve", "deny"):
            raise ValueError("Unknown approval decision")
        entry = self._store.decide_approval(
            identity,
            status="approved" if decision == "approve" else "denied",
            principal_id=principal_id,
        )
        waiter = self._waiters.get(identity)
        if waiter is not None and not waiter[1].done():
            waiter[1].set_result(decision == "approve")
        return entry

    def cancel_session(self, session_id: str) -> None:
        for identity, (bound_session, future) in tuple(self._waiters.items()):
            if bound_session == session_id:
                if self._store.approval(identity)["status"] == "pending":
                    self._store.decide_approval(identity, status="expired")
                if not future.done():
                    future.set_result(False)
