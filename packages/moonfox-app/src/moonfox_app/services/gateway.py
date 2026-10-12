"""One metered provider gate shared by session, continuation and summary requests."""

import asyncio
from copy import deepcopy
from dataclasses import replace

from moonfox_ai.api import (
    AssistantMessage,
    AssistantMessageEventStream,
    Done,
    Error,
    Model,
    Start,
    StreamFn,
    StreamOptions,
    StreamOverflowError,
    TranscriptContext,
    Usage,
    collapse_system_messages,
    estimate_message_tokens,
    now_ms,
)

from .store import HostStore


async def _close_stream(stream: AssistantMessageEventStream) -> None:
    task = asyncio.create_task(stream.aclose(), name="moonfox-provider-settle")
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancelled = exc
            if task.done():
                task.result()
                break
    if cancelled is not None:
        raise cancelled


class ModelGateway:
    def __init__(self, store: HostStore, *, concurrency: int) -> None:
        self._store = store
        self._semaphore = asyncio.Semaphore(concurrency)

    def bind(self, logical_id: str, provider: StreamFn) -> StreamFn:
        def stream(
            model: Model, context: TranscriptContext, options: StreamOptions | None = None
        ) -> AssistantMessageEventStream:
            output = AssistantMessageEventStream()
            snapshot = deepcopy(context)
            settings = replace(options) if options is not None else StreamOptions()

            async def produce() -> None:
                upstream: AssistantMessageEventStream | None = None
                acquired = False
                terminal: Done | Error | None = None
                started = False
                try:
                    # Poll semaphore cooperatively; cancelled waiters never invoke a provider.
                    while not acquired:
                        if settings.signal and settings.signal.aborted:
                            message = AssistantMessage(
                                [],
                                model.api,
                                model.provider,
                                model.id,
                                Usage(),
                                "aborted",
                                now_ms(),
                            )
                            output.push(Error("aborted", message))
                            return
                        try:
                            await asyncio.wait_for(self._semaphore.acquire(), 0.05)
                            acquired = True
                        except TimeoutError:
                            continue
                    if settings.signal and settings.signal.aborted:
                        message = AssistantMessage(
                            [], model.api, model.provider, model.id, Usage(), "aborted", now_ms()
                        )
                        output.push(Error("aborted", message))
                        return
                    estimated = sum(
                        estimate_message_tokens(message)
                        for message in collapse_system_messages(snapshot.messages)
                    )
                    maximum = settings.max_tokens or model.max_tokens or 2048
                    reserved = self._store.reserve_request(logical_id, estimated, maximum)
                    upstream = provider(model, snapshot, replace(settings, max_tokens=reserved))
                    try:
                        async for event in upstream:
                            if isinstance(event, (Done, Error)):
                                terminal = deepcopy(event)
                            else:
                                started = started or isinstance(event, Start)
                                output.push(deepcopy(event))
                    except StreamOverflowError:
                        final = await upstream.result()
                        if final.stop_reason in ("error", "aborted"):
                            terminal = Error(final.stop_reason, final)
                        elif final.stop_reason in ("stop", "length", "tool_use", "deferred"):
                            terminal = Done(final.stop_reason, final)
                        if not started and isinstance(terminal, Done):
                            output.push(Start(deepcopy(final)))
                    final = await upstream.result()
                    # Reservations are conservative. Actual usage can still exceed a heuristic;
                    # charge any excess before permitting another request, never refund a crash.
                    excess = max(0, final.usage.effective_total - estimated - reserved)
                    if excess:
                        self._store.charge_tokens(logical_id, excess)
                finally:
                    try:
                        if upstream is not None:
                            await _close_stream(upstream)
                    finally:
                        if acquired:
                            self._semaphore.release()
                if terminal is not None:
                    output.push(terminal)  # Complete only after upstream resources settle.

            output.start(produce())
            return output

        return stream
