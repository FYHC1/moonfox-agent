"""Provider contracts without dependencies on a concrete cancellation implementation."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..stream import AssistantMessageEventStream
from ..types import Model, TranscriptContext


class AbortSignal(Protocol):
    @property
    def aborted(self) -> bool: ...

    async def wait(self) -> None: ...


@dataclass(slots=True)
class ProviderResponse:
    status: int
    headers: dict[str, str]
    body: Any = None


@dataclass(slots=True)
class StreamOptions:
    signal: AbortSignal | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning: str | None = None
    session_id: str | None = None
    cache_retention: Literal["short", "long"] = "short"
    headers: dict[str, str] | None = None
    on_payload: Callable[[Any, Model], Any] | None = None
    on_response: Callable[[ProviderResponse, Model], None] | None = None


class StreamFn(Protocol):
    """Return a stream for every operational outcome, including failure/abort.

    Authentication, transport, model errors and cancellation terminate with Error.
    API misuse and programming errors are exceptions, never retryable model errors.
    Prompt and tool declarations must already be folded into TranscriptContext.
    """

    def __call__(
        self,
        model: Model,
        context: TranscriptContext,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream: ...
