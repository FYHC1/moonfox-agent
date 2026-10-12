"""Run-scoped ports for a long-lived session runtime; idle extensions cannot spend a run budget."""

from moonfox_ai.api import (
    AssistantMessageEventStream,
    Model,
    StreamFn,
    StreamOptions,
    TranscriptContext,
)
from moonfox_app.extension_api import PermissionChecker, PermissionDecision, ToolPermissionRequest

from .errors import BudgetExceeded


class RunPorts:
    def __init__(self) -> None:
        self._stream: StreamFn | None = None
        self._permission: PermissionChecker | None = None

    def bind(self, stream: StreamFn, permission: PermissionChecker) -> None:
        self._stream = stream
        self._permission = permission

    def revoke(self) -> None:
        self._stream = None
        self._permission = None

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream:
        if self._stream is None:
            raise BudgetExceeded("No active logical input budget")
        return self._stream(model, context, options)

    async def permission(self, request: ToolPermissionRequest) -> PermissionDecision:
        if self._permission is None:
            return PermissionDecision(False, "No active host run")
        return await self._permission(request)
