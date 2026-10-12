"""Explicit composition root for one headless session. No discovery or workers on import."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from moonfox_agent.api import AgentTool, RunOptions, RunOutcome
from moonfox_ai.api import Model, StreamFn, UserMessage

from .extension_api import (
    CustomEntryCodec,
    Delivery,
    EventHandler,
    ExtensionDiagnostic,
    PermissionChecker,
    RegistrationHandle,
    SessionBinding,
    SessionView,
)
from .extensions import ExtensionRegistry, SessionExtensionManager
from .runtime.compaction import CompactionPolicy, CompactionResult, CompactionSettings
from .runtime.prompt import PromptPipelineOptions
from .runtime.session.manager import SessionManager
from .runtime.session.orchestration import AgentSession
from .runtime.storage import SessionStore


class HeadlessApp:
    """Small composition handle, not a multi-session hub or a background worker."""

    def __init__(self, session: AgentSession, extensions: SessionExtensionManager) -> None:
        self._session = session
        self._extensions = extensions
        self._closing = False
        self._closed = False
        self._maintenance = False
        self._close_task: asyncio.Task[None] | None = None

    def _require_open(self) -> None:
        if self._closing or self._closed or self._maintenance:
            raise RuntimeError("Headless app is closed, closing or changing extensions")

    @property
    def active_extensions(self) -> tuple[str, ...]:
        return self._extensions.active_ids

    @property
    def diagnostics(self) -> tuple[ExtensionDiagnostic, ...]:
        return (*self._session.diagnostics, *self._extensions.diagnostics)

    @property
    def recovered_tool_calls(self) -> tuple[str, ...]:
        return self._session.recovered_tool_calls

    @property
    def is_running(self) -> bool:
        return self._session.is_running

    def view(self) -> SessionView:
        return self._session.view()

    def subscribe(self, listener: EventHandler) -> RegistrationHandle:
        self._require_open()
        return self._session.subscribe(listener)

    async def prompt(self, message: str | UserMessage, *, options: RunOptions) -> RunOutcome:
        self._require_open()
        return await self._session.prompt(message, options=options)

    async def continue_(self, *, options: RunOptions) -> RunOutcome:
        self._require_open()
        return await self._session.continue_(options=options)

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        self._require_open()
        self._session.enqueue(message, delivery)

    def abort(self) -> None:
        self._session.abort()

    async def wait_for_idle(self) -> None:
        await self._session.wait_for_idle()

    async def compact(self, *, instructions: str | None = None) -> CompactionResult | None:
        self._require_open()
        return await self._session.compact(instructions=instructions)

    def set_model(self, model: Model) -> None:
        self._require_open()
        self._session.set_model(model)

    def set_thinking_level(self, level: str) -> None:
        self._require_open()
        self._session.set_thinking_level(level)

    def select_leaf(self, leaf_id: str) -> None:
        self._require_open()
        self._session.select_leaf(leaf_id)

    async def disable_extension(self, extension_id: str) -> None:
        self._require_open()
        self._session.ensure_idle()
        self._maintenance = True
        try:
            await self._extensions.disable(extension_id)
            await self._session.refresh_extensions()
        finally:
            self._maintenance = False

    async def _close(self) -> None:
        self._session.abort()
        await self._session.wait_for_idle()
        try:
            await self._extensions.stop()
            await self._session.refresh_extensions()
        finally:
            await self._session.aclose()
            self._closed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._maintenance:
            raise RuntimeError("Wait for extension maintenance before closing")
        self._session.ensure_external_operation()
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="moonfox-headless-close")
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError as exc:
                cancelled = exc
                if self._close_task.done():
                    self._close_task.result()
                    break
        if cancelled is not None:
            raise cancelled

    async def __aenter__(self) -> HeadlessApp:
        self._require_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


async def create_headless_app(
    *,
    store: SessionStore,
    binding: SessionBinding,
    stream_fn: StreamFn,
    model: Model | None = None,
    system_prompt: str = "",
    thinking_level: str = "off",
    tools: Sequence[AgentTool] = (),
    permission_checker: PermissionChecker | None = None,
    registry: ExtensionRegistry | None = None,
    enabled_extensions: Sequence[str] = (),
    grants: Mapping[str, frozenset[str]] | None = None,
    codecs: Mapping[str, CustomEntryCodec] | None = None,
    prompt_options: PromptPipelineOptions | None = None,
    compaction_policy: CompactionPolicy | None = None,
    compaction_settings: CompactionSettings | None = None,
    callback_timeout: float = 5.0,
) -> HeadlessApp:
    """Takes store ownership, including failure cleanup. No fixed path/config is read.

    model/prompt/thinking initialize a new store only; restored history owns these values.
    Host authorization grants are mandatory for every requested extension capability.
    """
    session: AgentSession | None = None
    manager: SessionExtensionManager | None = None
    try:
        if store.snapshot().entries:
            transcript = SessionManager(store, binding=binding, codecs=codecs)
        else:
            if model is None:
                raise ValueError("A new session requires an initial model")
            transcript = SessionManager.create(
                store,
                binding=binding,
                model=model,
                system_prompt=system_prompt,
                thinking_level=thinking_level,
                codecs=codecs,
            )
        session = AgentSession(
            transcript,
            stream_fn=stream_fn,
            tools=tools,
            permission_checker=permission_checker,
            prompt_options=prompt_options,
            compaction_policy=compaction_policy,
            compaction_settings=compaction_settings,
            notification_timeout=callback_timeout,
            permission_timeout=callback_timeout,
        )
        manager = SessionExtensionManager(
            registry or ExtensionRegistry(),
            actions=session,
            enabled=enabled_extensions,
            grants=grants,
            timeout=callback_timeout,
            ensure_idle=session.ensure_idle,
        )
        session.bind_extensions(manager)
        await manager.start()
        await session.refresh_extensions()
        return HeadlessApp(session, manager)
    except BaseException:
        try:
            if manager is not None:
                await manager.stop()
        finally:
            if session is not None:
                await session.aclose()
            else:
                store.close()
        raise
