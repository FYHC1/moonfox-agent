"""Optional httpx transport, loaded only when a configured remote provider is invoked."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import TYPE_CHECKING, Protocol

from moonfox_ai.api import AbortSignal, HTTPRequest, HTTPResponse

if TYPE_CHECKING:
    import httpx


class _Closable(Protocol):
    async def aclose(self) -> None: ...


async def close_response(response: _Closable) -> None:
    """Do not detach provider cleanup, even when the owner is cancelled repeatedly."""
    task = asyncio.create_task(response.aclose(), name="moonfox-http-close")
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


async def _cancelable[T](operation: Awaitable[T], signal: AbortSignal | None) -> T:
    if signal is None:
        return await operation
    worker = asyncio.ensure_future(operation)
    stopper = asyncio.create_task(signal.wait())
    try:
        ready, _ = await asyncio.wait({worker, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in ready and signal.aborted:
            raise asyncio.CancelledError
        return await worker
    finally:
        if not worker.done():
            worker.cancel()
        stopper.cancel()
        await asyncio.gather(worker, stopper, return_exceptions=True)


class _Response:
    def __init__(
        self, response: httpx.Response, client: httpx.AsyncClient, signal: AbortSignal | None
    ) -> None:
        self._response = response
        self._client = client
        self._signal = signal
        self._closed = False

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return dict(self._response.headers)

    async def iter_lines(self) -> AsyncIterator[str]:
        """Bound raw bytes before splitting; an unbounded line cannot allocate indefinitely."""
        if self._response.headers.get("content-encoding", "identity").lower() != "identity":
            raise OSError("Compressed provider responses are not supported")
        iterator = self._response.aiter_raw().__aiter__()
        buffer = bytearray()
        total = 0
        try:
            while True:
                try:
                    chunk = await _cancelable(anext(iterator), self._signal)
                except StopAsyncIteration:
                    break
                total += len(chunk)
                if total > 16 * 1024 * 1024:
                    raise OSError("Provider response exceeds size limit")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    if len(line) > 1024 * 1024:
                        raise OSError("Provider frame exceeds size limit")
                    buffer = bytearray(rest)
                    yield line.rstrip(b"\r").decode("utf-8")
                if len(buffer) > 1024 * 1024:
                    raise OSError("Provider frame exceeds size limit")
            if buffer:
                yield bytes(buffer).rstrip(b"\r").decode("utf-8")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OSError("Provider response could not be read") from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


class HttpxTransport:
    """No client or sockets at import/construction. Caller owns each returned response."""

    async def __call__(self, request: HTTPRequest, signal: AbortSignal | None) -> HTTPResponse:
        try:
            import httpx
        except ImportError:
            raise OSError("Install moonfox-app[providers] to use network providers") from None
        if signal is not None and signal.aborted:
            raise asyncio.CancelledError
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(request.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )
        response: httpx.Response | None = None
        try:
            built = client.build_request(
                request.method,
                request.url,
                headers=request.headers,
                json=request.payload if request.payload is not None else None,
            )
            response = await client.send(built, stream=True)
            if signal is not None and signal.aborted:
                raise asyncio.CancelledError
            return _Response(response, client, signal)
        except BaseException as exc:
            if response is not None:
                await close_response(_Response(response, client, signal))
            else:
                await close_response(client)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, Exception):
                raise OSError("Provider connection failed") from None
            raise
