"""Injected async HTTP transport contracts. No HTTP library, configuration or startup required."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .base import AbortSignal


@dataclass(frozen=True, slots=True)
class HTTPRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    payload: dict[str, Any] | None = field(default=None, repr=False)
    timeout_seconds: float = 60.0


class HTTPResponse(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_lines(self) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


type HTTPTransport = Callable[[HTTPRequest, AbortSignal | None], Awaitable[HTTPResponse]]
