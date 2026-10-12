"""Authenticated resumable delivery with transport-owned tasks, never run ownership."""

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from moonfox_app.security.identity import AccessDenied, AuthenticationError, Principal, TokenAuth
from moonfox_app.services.errors import NotFoundError, ResyncRequired

from ._http import allowed_origin, response_model
from ._ports import ServerHub, Subscription
from .schema import Snapshot, Subscribe, WireEvent

AUTH_TIMEOUT_SECONDS = 5.0
HEARTBEAT_SECONDS = 15.0
MAX_FRAME_BYTES = 8 * 1024


class _RejectedFrame(Exception):
    def __init__(self, code: int = 1008) -> None:
        self.code = code


async def _settle_tasks(*tasks: asyncio.Task[Any]) -> None:
    """Repeated connection cancellation cannot leave subscription/receiver tasks behind."""
    for task in tasks:
        task.cancel()
    joined = asyncio.gather(*tasks, return_exceptions=True)
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            continue
    joined.result()


def _frame_text(message: Mapping[str, Any]) -> str:
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect
    text = message.get("text")
    data = message.get("bytes")
    size = len(text.encode("utf-8")) if isinstance(text, str) else len(data or b"")
    if size > MAX_FRAME_BYTES:
        raise _RejectedFrame(1009)
    if not isinstance(text, str):
        raise _RejectedFrame
    return text


async def _watch_client(websocket: WebSocket) -> int | None:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    try:
        _frame_text(message)
    except _RejectedFrame as exc:
        return exc.code
    # This stream is receive-only after the single authenticated Subscribe frame.
    return 1008


async def _send_resync(
    websocket: WebSocket, hub: ServerHub, principal: Principal, session_id: str
) -> None:
    snapshot = response_model(Snapshot, await hub.snapshot(principal, session_id))
    await websocket.send_json(
        WireEvent(
            epoch=snapshot.epoch,
            session_id=session_id,
            seq=snapshot.seq,
            type="resync_required",
            payload={"reason": "snapshot_required"},
        ).model_dump(mode="json")
    )


async def _send_events(
    websocket: WebSocket,
    hub: ServerHub,
    principal: Principal,
    session_id: str,
    command: Subscribe,
) -> int:
    subscription: Subscription | None = None
    pending: asyncio.Task[dict[str, Any]] | None = None
    epoch, seq = command.epoch, command.after_seq
    try:
        try:
            # No await between the service's access check, registration and replay capture.
            subscription = hub.subscribe(principal, session_id, epoch=epoch, after_seq=seq)
            while True:
                if pending is None:
                    pending = asyncio.create_task(subscription.next(), name="moonfox-ws-next")
                ready, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
                if not ready:
                    await websocket.send_json(
                        WireEvent(
                            epoch=epoch,
                            session_id=session_id,
                            seq=seq,
                            type="heartbeat",
                            payload={},
                        ).model_dump(mode="json")
                    )
                    continue
                event = response_model(WireEvent, pending.result())
                pending = None
                if event.session_id != session_id or event.epoch != epoch:
                    raise ResyncRequired
                if event.type == "resync_required":
                    raise ResyncRequired
                if event.type == "heartbeat":
                    continue
                if event.seq <= seq:
                    continue
                if event.seq != seq + 1:
                    raise ResyncRequired
                await websocket.send_json(event.model_dump(mode="json"))
                seq = event.seq
        except ResyncRequired:
            await _send_resync(websocket, hub, principal, session_id)
            return 1000
    finally:
        # Unsubscribe synchronously even when cancellation interrupts awaited cleanup.
        if subscription is not None:
            subscription.close()
        if pending is not None:
            await _settle_tasks(pending)


async def _close(websocket: WebSocket, code: int) -> None:
    with suppress(WebSocketDisconnect, RuntimeError, OSError):
        await websocket.close(
            code=code, reason="Subscription ended" if code == 1000 else "Rejected"
        )


async def serve_events(
    websocket: WebSocket,
    hub: ServerHub,
    auth: TokenAuth,
    origins: frozenset[str],
    session_id: str,
) -> None:
    try:
        allowed_origin(websocket.headers, origins, required=True)
        if websocket.scope.get("query_string"):
            raise AccessDenied
    except AccessDenied:
        await _close(websocket, 1008)
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(AUTH_TIMEOUT_SECONDS):
            command = Subscribe.model_validate_json(_frame_text(await websocket.receive()))
            identity = auth.authenticate(command.token)
        # Sender and receiver are both settled on every exit. Neither owns the underlying run.
        sender = asyncio.create_task(
            _send_events(websocket, hub, identity, session_id, command), name="moonfox-ws-sender"
        )
        receiver = asyncio.create_task(_watch_client(websocket), name="moonfox-ws-receiver")
        try:
            ready, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            code = receiver.result() if receiver in ready else sender.result()
        finally:
            await _settle_tasks(sender, receiver)
        if code is not None:
            await _close(websocket, code)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # ASGI connection cancellation is a disconnect, not cancellation of the Agent run.
        pass
    except _RejectedFrame as exc:
        await _close(websocket, exc.code)
    except (AuthenticationError, AccessDenied, NotFoundError, ValidationError, TimeoutError):
        await _close(websocket, 1008)
    except Exception:
        await _close(websocket, 1011)
