"""Session-bound background completion; shares durable input budgets and provider gates."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from moonfox_ai.api import (
    Context,
    StreamOptions,
    SystemMessage,
    TextContent,
    UserMessage,
    normalize_context,
    now_ms,
)
from moonfox_app.config import SessionSettings
from moonfox_app.extension_api import SessionBinding

from .errors import BudgetExceeded, ConflictError
from .gateway import ModelGateway, _close_stream
from .model_catalog import ModelCatalog
from .store import HostStore


class BoundExtensionModels:
    def __init__(
        self,
        *,
        store: HostStore,
        catalog: ModelCatalog,
        gateway: ModelGateway,
        binding: SessionBinding,
        extension_id: str,
        settings: SessionSettings,
        current_ticket: Callable[[], str | None],
        accepting: Callable[[], bool],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._gateway = gateway
        self._binding = binding
        self._extension_id = extension_id
        self._settings = settings
        self._ticket = current_ticket
        self._accepting = accepting
        self._revoked = False

    def revoke(self) -> None:
        self._revoked = True

    def _authorize(self) -> None:
        if self._revoked or not self._accepting():
            raise ConflictError("Extension model capability is closed")
        session = self._store.session(self._binding.session_id)
        if (
            session.scope_id != self._binding.scope_id
            or self._extension_id not in session.settings.enabled_extensions
        ):
            raise ConflictError("Extension model capability is no longer authorized")

    def current_ticket(self) -> str | None:
        self._authorize()
        return self._ticket()

    async def complete(self, ticket: str, *, system: str, text: str, max_tokens: int) -> str:
        self._authorize()
        if not isinstance(ticket, str) or not any(
            item.logical_id == ticket for item in self._store.inputs(self._binding.session_id)
        ):
            raise ConflictError("Budget ticket is not bound to this session")
        if (
            type(max_tokens) is not int
            or not 1 <= max_tokens <= 2048
            or not isinstance(system, str)
            or not isinstance(text, str)
            or not 1 <= len(system) <= 8000
            or not 1 <= len(text) <= 32000
        ):
            raise ValueError("Invalid bounded extension completion")
        budget = self._store.budget(ticket)
        remaining = budget.max_milliseconds - budget.milliseconds_used
        if remaining <= 0 or budget.requests_used >= budget.max_requests:
            raise BudgetExceeded("Originating input budget exhausted")
        # Credentials/parameters are frozen for this job request, never stored in the job DB.
        source = self._catalog.resolve(self._settings)
        stream = None
        start = time.monotonic()
        try:
            async with asyncio.timeout(remaining / 1000):
                stream = self._gateway.bind(ticket, source.stream_fn, defaults=source.options)(
                    source.model,
                    normalize_context(
                        Context([SystemMessage(system, now_ms()), UserMessage(text, now_ms())])
                    ),
                    StreamOptions(max_tokens=max_tokens),
                )
                async for _ in stream:
                    pass
                result = await stream.result()
                if result.stop_reason != "stop" or any(
                    block.type == "toolCall" for block in result.content
                ):
                    raise ValueError("Extension completion was not a complete text response")
                return "".join(
                    block.text for block in result.content if isinstance(block, TextContent)
                )
        finally:
            try:
                if stream is not None:
                    await _close_stream(stream)
            finally:
                # Conservative accounting: overlapping elapsed time is not refunded.
                self._store.charge_time(ticket, round((time.monotonic() - start) * 1000))
