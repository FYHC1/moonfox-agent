"""Shared request validation, live events, and owned transport lifetimes."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

from ..stream import AssistantMessageEventStream
from ..transcript import get_current_tools
from ..types import (
    AssistantMessage,
    Done,
    Error,
    Model,
    Start,
    SystemMessage,
    TextContent,
    TextDelta,
    TextEnd,
    TextStart,
    ThinkingContent,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UserMessage,
    now_ms,
)
from ._sse import InvalidResponse, SSEFrame, iter_sse
from ._tool_names import normalize_tool_names
from .base import AbortSignal, ProviderResponse, StreamOptions
from .http import HTTPRequest, HTTPResponse, HTTPTransport

_RESERVED_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "anthropic-version",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "te",
        "trailer",
        "upgrade",
        "cookie",
        "set-cookie",
        "content-encoding",
    }
)
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def _valid_key(key: str | None) -> None:
    if key is not None and (
        not isinstance(key, str) or any(ord(char) < 32 or ord(char) == 127 for char in key)
    ):
        raise ValueError("Invalid API credential")


def _base_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("Invalid provider base URL")
    try:
        parts = urlsplit(value)
        port = parts.port
        valid = (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (port is None or 0 < port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid provider base URL")
    return value.rstrip("/")


def number(value: float | None, minimum: float, maximum: float, name: str) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"Invalid {name}")


def max_tokens(model: Model, options: StreamOptions) -> int:
    value = options.max_tokens if options.max_tokens is not None else model.max_tokens
    if value is None:
        return 4096
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_tokens must be a positive integer")
    return value


def checked_options(options: StreamOptions | None) -> StreamOptions:
    if options is not None and not isinstance(options, StreamOptions):
        raise TypeError("Expected StreamOptions")
    result = replace(options, headers=deepcopy(options.headers)) if options else StreamOptions()
    _valid_key(result.api_key)
    if result.stop is not None and (
        not isinstance(result.stop, tuple)
        or any(not isinstance(item, str) or not item for item in result.stop)
    ):
        raise ValueError("stop must be a tuple of nonempty strings")
    return result


def request_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Request payload must be JSON-compatible") from None


def validate_context(model: Model, context: TranscriptContext, supports_tools: bool) -> None:
    if not isinstance(model, Model) or not isinstance(model.id, str) or not model.id.strip():
        raise TypeError("Expected a model with a nonempty ID")
    if not isinstance(context, TranscriptContext):
        raise TypeError("Provider requires normalized TranscriptContext")
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in context.messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, ToolResultMessage):
            if not supports_tools:
                raise ValueError("Provider does not support tools")
            if pending.get(message.tool_call_id) != message.tool_name:
                raise ValueError("Transcript tool results must match pending calls")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise ValueError("Transcript contains unanswered tool calls")
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if not isinstance(block, ToolCall):
                    continue
                if not supports_tools:
                    raise ValueError("Provider does not support tools")
                if (
                    not isinstance(block.id, str)
                    or not block.id.strip()
                    or not isinstance(block.name, str)
                    or not block.name.strip()
                    or block.id in seen
                    or not isinstance(block.arguments, dict)
                ):
                    raise ValueError("Transcript contains invalid or duplicate tool calls")
                request_json(block.arguments)
                seen.add(block.id)
                pending[block.id] = block.name
        elif not isinstance(message, UserMessage):
            raise TypeError("Transcript contains an unsupported message")
    if pending:
        raise ValueError("Transcript contains unanswered tool calls")
    tools = get_current_tools(context.messages)
    if tools and not supports_tools:
        raise ValueError("Provider does not support tools")
    for tool in tools:
        if not isinstance(tool.name, str) or not tool.name.strip():
            raise ValueError("Tool names must be nonempty")
        if not isinstance(tool.parameters, dict):
            raise ValueError("Tool schemas must be JSON objects")
        request_json(tool.parameters)


def wire_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidResponse()
    return cast(dict[str, Any], value)


def wire_string(value: Any, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise InvalidResponse()
    return value


def wire_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidResponse()
    return cast(int, value)


def _reject_constant(value: str) -> NoReturn:
    raise InvalidResponse()


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise InvalidResponse()
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse()
        result[key] = value
    return result


def json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, RecursionError):
        raise InvalidResponse() from None
    return wire_object(value)


class MessageState:
    """Append-only live blocks; only explicitly finalized calls survive a failure."""

    def __init__(
        self, output: AssistantMessageEventStream, model: Model, tool_names: dict[str, str]
    ) -> None:
        self.tool_names = tool_names
        self.output = output
        self.message = AssistantMessage(
            [], model.api, model.provider, model.id, Usage(), "pending", now_ms()
        )
        self.ended: set[int] = set()
        self.complete_calls: set[int] = set()
        self.call_ids: set[str] = set()

    def text_start(self, initial: str = "") -> int:
        index = len(self.message.content)
        self.message.content.append(TextContent(""))
        self.output.push(TextStart(index, self.message))
        if initial:
            self.text_delta(index, initial)
        return index

    def text_delta(self, index: int, delta: str) -> None:
        block = self.message.content[index]
        if not isinstance(block, TextContent) or index in self.ended:
            raise InvalidResponse()
        block.text += delta
        self.output.push(TextDelta(index, delta, self.message))

    def thinking_start(self, initial: str = "") -> int:
        index = len(self.message.content)
        self.message.content.append(ThinkingContent(""))
        self.output.push(ThinkingStart(index, self.message))
        if initial:
            self.thinking_delta(index, initial)
        return index

    def thinking_delta(self, index: int, delta: str) -> None:
        block = self.message.content[index]
        if not isinstance(block, ThinkingContent) or index in self.ended:
            raise InvalidResponse()
        block.thinking += delta
        self.output.push(ThinkingDelta(index, delta, self.message))

    def tool_start(self, call_id: str = "", name: str = "") -> int:
        index = len(self.message.content)
        self.message.content.append(ToolCall(call_id, name, {}))
        self.output.push(ToolCallStart(index, self.message))
        return index

    def tool_delta(self, index: int, delta: str) -> None:
        self.output.push(ToolCallDelta(index, delta, self.message))

    def tool_end(self, index: int, arguments: dict[str, Any]) -> None:
        block = self.message.content[index]
        if not isinstance(block, ToolCall) or index in self.ended:
            raise InvalidResponse()
        wire_string(block.id, nonempty=True)
        wire_string(block.name, nonempty=True)
        block.name = self.tool_names.get(block.name, block.name)
        if block.id in self.call_ids:
            raise InvalidResponse()
        self.call_ids.add(block.id)
        block.arguments = arguments
        self.complete_calls.add(id(block))
        self.ended.add(index)
        self.output.push(ToolCallEnd(index, block, self.message))

    def end(self, index: int) -> None:
        if index in self.ended:
            raise InvalidResponse()
        block = self.message.content[index]
        if isinstance(block, TextContent):
            self.output.push(TextEnd(index, block.text, self.message))
        elif isinstance(block, ThinkingContent):
            self.output.push(ThinkingEnd(index, block.thinking, self.message))
        else:
            raise InvalidResponse()
        self.ended.add(index)

    def discard_incomplete_calls(self) -> None:
        self.message.content[:] = [
            block
            for block in self.message.content
            if not isinstance(block, ToolCall) or id(block) in self.complete_calls
        ]


type Consume = Callable[[AsyncIterator[SSEFrame], MessageState], Awaitable[None]]


class RemoteProvider:
    """Immutable private settings; constructing a provider performs no I/O."""

    __slots__ = (
        "_transport",
        "_url",
        "__api_key",
        "_timeout",
        "_supports_tools",
        "_supports_images",
    )

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        supports_tools: bool,
        supports_images: bool,
    ) -> None:
        if not callable(transport):
            raise TypeError("Expected an HTTP transport")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (float, int))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if not isinstance(supports_tools, bool) or not isinstance(supports_images, bool):
            raise TypeError("Capabilities must be boolean")
        _valid_key(api_key)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_url", _base_url(base_url))
        object.__setattr__(self, "_RemoteProvider__api_key", api_key)
        object.__setattr__(self, "_timeout", timeout_seconds)
        object.__setattr__(self, "_supports_tools", supports_tools)
        object.__setattr__(self, "_supports_images", supports_images)

    _transport: HTTPTransport
    _url: str
    __api_key: str | None
    _timeout: float
    _supports_tools: bool
    _supports_images: bool

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("Provider configuration is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Provider configuration is immutable")

    def _start(
        self,
        endpoint: str,
        model: Model,
        options: StreamOptions,
        payload: dict[str, Any],
        consume: Consume,
        *,
        anthropic: bool = False,
        responses: bool = False,
    ) -> AssistantMessageEventStream:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        seen: set[str] = set()
        for name, value in (options.headers or {}).items():
            if (
                not isinstance(name, str)
                or _HEADER_NAME.fullmatch(name) is None
                or name.lower() in _RESERVED_HEADERS
                or name.lower() in seen
                or not isinstance(value, str)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError("Invalid or reserved custom HTTP header")
            seen.add(name.lower())
            headers[name] = value
        key = options.api_key if options.api_key is not None else self.__api_key
        if anthropic:
            headers["anthropic-version"] = "2023-06-01"
            if key:
                headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        tool_names = normalize_tool_names(payload, anthropic=anthropic, responses=responses)
        if options.on_payload is not None:
            payload = deepcopy(payload)
            replacement = options.on_payload(payload, model)
            if replacement is not None:
                if not isinstance(replacement, dict):
                    raise TypeError("on_payload must return an object or None")
                payload = replacement
        request_json(payload)
        request = HTTPRequest(
            "POST",
            f"{self._url}/{endpoint}",
            MappingProxyType(headers),
            deepcopy(payload),
            self._timeout,
        )
        output = AssistantMessageEventStream()
        secrets = tuple(value for value in (key, self.__api_key) if value)
        output.start(
            _produce(self._transport, request, model, options, consume, output, secrets, tool_names)
        )
        return output


class _Aborted(Exception):
    pass


class _HTTPFailure(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__("Provider HTTP failure")


class _HookFailure(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__("Response hook failed")


class _Session(AsyncIterator[str]):
    def __init__(self, transport: HTTPTransport, signal: AbortSignal | None) -> None:
        self.transport = transport
        self.signal = signal
        self.watcher: asyncio.Task[None] | None = None
        self.opening: asyncio.Task[HTTPResponse] | None = None
        self.reading: asyncio.Task[str] | None = None
        self.iterator: AsyncIterator[str] | None = None
        self.response: HTTPResponse | None = None

    async def _race[T](self, task: asyncio.Task[T]) -> T:
        if self.signal is None:
            return await asyncio.shield(task)
        if self.signal.aborted:
            raise _Aborted()
        if self.watcher is None:
            self.watcher = asyncio.create_task(self.signal.wait())
        await asyncio.wait((task, self.watcher), return_when=asyncio.FIRST_COMPLETED)
        if self.watcher.done():
            self.watcher.result()
            raise _Aborted()
        if self.signal.aborted:
            raise _Aborted()
        return task.result()

    async def open(self, request: HTTPRequest) -> HTTPResponse:
        if self.signal is not None and self.signal.aborted:
            raise _Aborted()

        async def opening() -> HTTPResponse:
            return await self.transport(request, self.signal)

        self.opening = asyncio.create_task(opening())
        self.response = await self._race(self.opening)
        return self.response

    def __aiter__(self) -> _Session:
        return self

    async def __anext__(self) -> str:
        if self.iterator is None:
            assert self.response is not None
            self.iterator = self.response.iter_lines()

        async def reading() -> str:
            assert self.iterator is not None
            return await anext(self.iterator)

        self.reading = asyncio.create_task(reading())
        return await self._race(self.reading)

    async def close(self) -> None:
        tasks = [task for task in (self.reading, self.opening, self.watcher) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # An opening transport may return a response while handling cancellation.
        if self.response is None and self.opening is not None and not self.opening.cancelled():
            if self.opening.exception() is None:
                self.response = self.opening.result()
        try:
            if isinstance(self.iterator, AsyncGenerator):
                await self.iterator.aclose()
        finally:
            if self.response is not None:
                await self.response.aclose()


def _response_info(response: HTTPResponse, secrets: tuple[str, ...]) -> ProviderResponse:
    # No body, cookies or authentication headers cross the callback boundary.
    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        if name.lower() in {"content-type", "retry-after", "request-id", "x-request-id"}:
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            headers[name.lower()] = value
    return ProviderResponse(response.status, headers)


async def _produce(
    transport: HTTPTransport,
    request: HTTPRequest,
    model: Model,
    options: StreamOptions,
    consume: Consume,
    output: AssistantMessageEventStream,
    secrets: tuple[str, ...],
    tool_names: dict[str, str],
) -> None:
    state = MessageState(output, model, tool_names)
    session = _Session(transport, options.signal)
    problem: tuple[str, str, int | None] | None = None
    bug: Exception | None = None
    aborted = False
    try:
        async with asyncio.timeout(request.timeout_seconds):
            response = await session.open(request)
            status = response.status
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                raise InvalidResponse()
            if options.on_response is not None:
                try:
                    options.on_response(_response_info(response, secrets), model)
                except Exception as exc:
                    raise _HookFailure(exc) from None
            if not 200 <= status < 300:
                raise _HTTPFailure(status)
            output.push(Start(state.message))
            async with aclosing(iter_sse(session)) as frames:
                await consume(frames, state)
            if state.message.stop_reason not in ("stop", "length", "tool_use", "deferred"):
                raise InvalidResponse()
    except (asyncio.CancelledError, _Aborted):
        aborted = True
    except _HTTPFailure as exc:
        if exc.status in (401, 403):
            text = "Provider authentication failed"
        elif exc.status == 429:
            text = "Provider rate limit exceeded"
        elif exc.status >= 500:
            text = "Provider service unavailable"
        else:
            text = "Provider request failed"
        problem = (text, "http_error", exc.status)
    except (InvalidResponse, UnicodeError):
        problem = ("Invalid provider response", "invalid_response", None)
    except TimeoutError:
        problem = ("Provider request timed out", "timeout", None)
    except OSError:
        problem = ("Provider connection failed", "transport_error", None)
    except _HookFailure as exc:
        bug = exc.error
    except Exception as exc:
        bug = exc
    # Shield the entire join/close sequence, not only the final response close.
    # Repeated consumer cancellations cannot detach any owned operation.
    cleanup = asyncio.create_task(session.close())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            aborted = True
        except Exception:
            break
    try:
        cleanup.result()
    except asyncio.CancelledError:
        aborted = True
    except OSError:
        problem = problem or ("Provider connection failed", "transport_error", None)
    except Exception as exc:
        bug = bug or exc
    if options.signal is not None and options.signal.aborted:
        aborted = True
    if bug is not None:
        output.fail(bug)
        return
    if aborted or problem is not None:
        state.discard_incomplete_calls()
        state.message.stop_reason = "aborted" if aborted else "error"
        if aborted:
            state.message.error_message = "Operation aborted"
        else:
            assert problem is not None
            state.message.error_message = problem[0]
        if problem is not None and not aborted:
            diagnostic: dict[str, Any] = {"code": problem[1]}
            if problem[2] is not None:
                diagnostic["status"] = problem[2]
            state.message.diagnostics = [diagnostic]
        output.push(Error("aborted" if aborted else "error", state.message))
    else:
        reason = state.message.stop_reason
        assert reason in ("stop", "length", "tool_use", "deferred")
        output.push(Done(reason, state.message))
