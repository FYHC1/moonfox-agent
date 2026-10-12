"""Bounded, line-oriented SSE decoding for injected transports."""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass


class InvalidResponse(Exception):
    """Untrusted wire data violated the protocol; never retain that data here."""

    def __init__(self) -> None:
        super().__init__("Invalid provider response")


@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str
    data: str


async def iter_sse(
    lines: AsyncIterator[str],
    *,
    max_frame_bytes: int = 1024 * 1024,
    max_total_bytes: int = 16 * 1024 * 1024,
) -> AsyncGenerator[SSEFrame]:
    """Require a blank-line terminator, including for the last dispatched frame.

    The transport supplies individual lines, either stripped or ending in LF/CRLF.
    Comments and extension fields count toward the bounds even when discarded.
    """
    if max_frame_bytes < 1 or max_total_bytes < max_frame_bytes:
        raise ValueError("Invalid SSE bounds")
    data: list[str] = []
    event = "message"
    frame_bytes = total_bytes = 0
    pending = False
    first = True
    async for raw in lines:
        if not isinstance(raw, str) or len(raw) > max_frame_bytes:
            raise InvalidResponse()
        try:
            size = len(raw.encode("utf-8")) + 1
        except UnicodeError:
            raise InvalidResponse() from None
        frame_bytes += size
        total_bytes += size
        if frame_bytes > max_frame_bytes or total_bytes > max_total_bytes:
            raise InvalidResponse()
        line = raw.removesuffix("\n").removesuffix("\r")
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        if "\n" in line or "\r" in line or "\x00" in line:
            raise InvalidResponse()
        if not line:
            if data:
                yield SSEFrame(event, "\n".join(data))
            data = []
            event = "message"
            frame_bytes = 0
            pending = False
            continue
        if line.startswith(":"):
            continue
        pending = True
        name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value or "message"
        # SSE id/retry and unknown extension fields have no effect on this request.
    if pending or data:
        raise InvalidResponse()
