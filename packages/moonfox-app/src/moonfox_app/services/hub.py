"""Durable input admission and independent single-session actors. No transport imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from moonfox_agent.api import RunOptions
from moonfox_ai.api import StreamFn
from moonfox_app.config import HostSettings, SessionSettings
from moonfox_app.config.providers import ModelSettings, ProviderSettings
from moonfox_app.extension_api import (
    ExtensionManifest,
    InputReceipt,
    PermissionChecker,
    PermissionDecision,
    RegistrationHandle,
    SessionBinding,
    SessionEvent,
    SessionView,
    ToolPermissionRequest,
)
from moonfox_app.security import AccessDenied, Principal, authorize_session
from moonfox_app.security.approvals import Approvals

from .channel import BoundChannelGateway, ChannelBinding
from .errors import BudgetExceeded, ConflictError, InvalidConfiguration
from .events import EventBus, Subscription
from .gateway import ModelGateway
from .model_catalog import ModelCatalog
from .readmodel import session_update, view_messages
from .session_factory import RunPorts
from .store import HostStore
from .types import InputRecord, ModelRegistration, RuntimeFactory, SessionRecord, SessionRuntime


@dataclass(slots=True)
class _Actor:
    runtime: SessionRuntime | None = None
    cancelled: bool = False
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_id: str | None = None


@dataclass(slots=True)
class _LiveSession:
    runtime: SessionRuntime
    ports: RunPorts
    revision: int
    model_revision: str = "builtin"


class SessionHub:
    def __init__(
        self,
        store: HostStore,
        *,
        models: Mapping[str, ModelRegistration],
        runtime_factory: RuntimeFactory,
        load_view: Callable[[SessionRecord], SessionView],
        tools: Sequence[str] = (),
        extensions: Sequence[ExtensionManifest] = (),
        validate_extensions: Callable[[tuple[str, ...]], None] | None = None,
        compact_enabled: bool = False,
        model_catalog: ModelCatalog | None = None,
    ) -> None:
        self._catalog = model_catalog or ModelCatalog(store, models)
        self._store = store
        self._models = dict(models)
        self._factory = runtime_factory
        self._load_view = load_view
        self._tools = tuple(tools)
        self._extensions = tuple(extensions)
        self._validate_extensions = validate_extensions
        self._compact_enabled = compact_enabled
        _, self._startup_settings = store.config()
        self._validate_settings(self._startup_settings.defaults, available=False)
        self.events = EventBus(
            window=self._startup_settings.event_window,
            capacity=self._startup_settings.subscriber_buffer,
        )
        self._gateway = ModelGateway(
            store, concurrency=self._startup_settings.max_concurrent_requests
        )
        self._approvals = Approvals(
            store, self.events.publish, timeout=self._startup_settings.approval_timeout_seconds
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._actors: dict[str, _Actor] = {}
        self._views: dict[str, SessionView] = {}
        self._unavailable: set[str] = set()
        self._started = False
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._lives: dict[str, _LiveSession] = {}
        self._channels: list[BoundChannelGateway] = []

    def _validate_settings(self, settings: SessionSettings, *, available: bool = True) -> None:
        SessionSettings.model_validate_json(settings.model_dump_json())
        self._catalog.validate_session(settings, available=available)
        if not set(settings.allowed_tools) <= set(self._tools):
            raise ValueError("Tools must be registered and authorized by the host")
        enabled = set(settings.enabled_extensions)
        if not enabled <= {
            manifest.id for manifest in self._extensions if manifest.scope == "session"
        }:
            raise ValueError("Extensions must be registered with this host")
        if self._validate_extensions is not None:
            self._validate_extensions(settings.enabled_extensions)

    def _require_started(self) -> None:
        if not self._started or self._closing or self._closed:
            raise ConflictError("Host is not accepting commands")

    def _authorized(self, principal: Principal, session_id: str) -> SessionRecord:
        self._require_started()
        session = self._store.session(session_id)
        authorize_session(principal, session.owner_id)
        return session

    async def start(self) -> None:
        if self._closed or self._closing:
            raise ConflictError("Host is closed")
        if self._started:
            return
        self._store.recover()
        for session in self._store.sessions():
            try:
                self._views[session.id] = self._load_view(session)
            except Exception:
                self._unavailable.add(session.id)
                for item in self._store.pending(session.id):
                    self._store.finish(
                        item.id, status="unknown", error="Session storage unavailable"
                    )
        self._started = True
        for session in self._store.sessions():
            self._kick(session.id)

    def _summary(self, session: SessionRecord) -> dict[str, Any]:
        inputs = self._store.inputs(session.id)
        status = (
            "error"
            if session.id in self._unavailable
            else "running"
            if session.id in self._actors
            else (inputs[-1].status if inputs else "idle")
        )
        return {
            "id": session.id,
            "title": session.title,
            "domain": session.domain,
            "owner_id": session.owner_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "status": status,
            "pending_count": len(self._store.pending(session.id)),
            "model_id": session.settings.model_id,
        }

    def _input_view(self, item: InputRecord) -> dict[str, Any]:
        budget = self._store.budget(item.logical_id)
        return {
            "id": item.id,
            "request_id": item.request_id,
            "run_id": item.run_id,
            "logical_id": item.logical_id,
            "status": item.status,
            "kind": item.kind,
            "created_at": item.created_at,
            "error": item.error,
            "requests_used": budget.requests_used,
            "tokens_used": budget.tokens_used,
            "max_requests": budget.max_requests,
        }

    @staticmethod
    def _receipt(item: InputRecord, request_id: str | None = None) -> dict[str, Any]:
        return {
            "request_id": request_id or item.request_id,
            "session_id": item.session_id,
            "input_id": item.id,
            "run_id": item.run_id,
            "status": item.status,
        }

    async def list_sessions(self, principal: Principal) -> list[dict[str, Any]]:
        self._require_started()
        return [
            self._summary(session)
            for session in self._store.sessions()
            if principal.is_admin or session.owner_id == principal.id
        ]

    async def create_session(
        self, principal: Principal, *, title: str, request_id: str
    ) -> dict[str, Any]:
        self._require_started()
        self._text(title, "title", 120)
        self._text(request_id, "request_id", 128)
        settings = self._store.config()[1].defaults
        self._validate_settings(settings)
        session = self._store.create_session(
            owner_id=principal.id,
            domain="agent",
            title=title,
            request_id=request_id,
            settings=settings,
        )
        if session.id not in self._views:
            self._views[session.id] = self._load_view(session)
        return self._summary(session)

    @staticmethod
    def _text(value: str, name: str, maximum: int) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"Invalid {name}")

    async def snapshot(self, principal: Principal, session_id: str) -> dict[str, Any]:
        session = self._authorized(principal, session_id)
        if session_id in self._unavailable:
            raise ConflictError("Session storage is unavailable")
        actor = self._actors.get(session_id)
        messages = (
            list(actor.messages.values()) if actor else view_messages(self._views[session_id])
        )
        # Snapshot and event watermark are captured without yielding to another actor.
        epoch, seq = self.events.watermark(session_id)
        enabled = session.settings.enabled_extensions
        capabilities = ["cancel", "configure", "approve"]
        if session.domain == "agent":
            capabilities += ["prompt", "continue"]
            if self._compact_enabled:
                capabilities.append("compact")
        return {
            "protocol_version": 1,
            "epoch": epoch,
            "session_id": session_id,
            "seq": seq,
            "session": self._summary(session),
            "messages": deepcopy(messages),
            "inputs": [self._input_view(item) for item in self._store.inputs(session_id)],
            "approvals": self._store.approvals(session_id),
            "settings": session.settings.model_dump(mode="json"),
            "config_revision": session.config_revision,
            "extensions": [
                {
                    "id": m.id,
                    "version": m.version,
                    "scope": m.scope,
                    "capabilities": list(m.capabilities),
                    "enabled": m.id in enabled,
                }
                for m in self._extensions
                if m.scope == "session"
            ],
            "capabilities": capabilities,
        }

    def subscribe(
        self, principal: Principal, session_id: str, *, epoch: str, after_seq: int
    ) -> Subscription:
        self._authorized(principal, session_id)
        return self.events.subscribe(session_id, epoch=epoch, after_seq=after_seq)

    async def submit_input(
        self,
        principal: Principal,
        session_id: str,
        *,
        text: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session = self._authorized(principal, session_id)
        if session.domain != "agent":
            raise AccessDenied("Channel input requires an authenticated channel capability")
        return self._submit(session, text=text, request_id=request_id, key=idempotency_key)

    def _submit(
        self, session: SessionRecord, *, text: str, request_id: str, key: str
    ) -> dict[str, Any]:
        if session.id in self._unavailable:
            raise ConflictError("Session storage is unavailable")
        self._text(text, "text", 32000)
        self._text(request_id, "request_id", 128)
        self._text(key, "idempotency_key", 128)
        item, fresh = self._store.enqueue(
            session,
            text=text,
            request_id=request_id,
            key=key,
            max_pending=self._startup_settings.max_pending_inputs,
        )
        if fresh:
            self.events.publish(session.id, "input_received", {"input": self._input_view(item)})
        self._kick(session.id)
        return self._receipt(item, request_id)

    async def control(
        self,
        principal: Principal,
        session_id: str,
        *,
        action: str,
        request_id: str,
        input_id: str | None = None,
    ) -> dict[str, Any]:
        session = self._authorized(principal, session_id)
        self._text(request_id, "request_id", 128)
        if action == "cancel":
            self._store.cancel_pending(session_id)
            actor = self._actors.get(session_id)
            if actor:
                actor.cancelled = True
                if actor.runtime:
                    actor.runtime.abort()
            self._approvals.cancel_session(session_id)
            self.events.publish(session_id, "session_changed", {})
            return {
                "request_id": request_id,
                "session_id": session_id,
                "input_id": input_id,
                "run_id": None,
                "status": "cancelling" if actor else "aborted",
            }
        if session.domain != "agent":
            raise AccessDenied("Channel runs cannot be started by viewing their session")
        duplicate = self._store.duplicate(session_id, request_id, request_id)
        if duplicate:
            if duplicate.kind != action:
                raise ConflictError("Control request ID conflict")
            return self._receipt(duplicate)
        if session_id in self._actors or self._store.pending(session_id):
            raise ConflictError("Session is busy")
        if action == "continue":
            if input_id is None:
                raise ValueError("Continuation requires an input_id")
            original = self._store.input(input_id)
            if original.session_id != session_id:
                raise AccessDenied("Input is bound to another session")
            history = self._store.inputs(session_id)
            if history[-1].id != original.id or original.status not in (
                "limit_reached",
                "aborted",
                "error",
            ):
                raise ConflictError("Only the latest settled recoverable input can continue")
            budget = self._store.budget(original.logical_id)
            if (
                budget.requests_used >= budget.max_requests
                or budget.tokens_used >= budget.max_tokens
                or budget.milliseconds_used >= budget.max_milliseconds
            ):
                raise BudgetExceeded("Logical input budget exhausted")
            conversation = [
                message for message in self._views[session_id].messages if message.role != "system"
            ]
            if not conversation or conversation[-1].role not in ("user", "toolResult"):
                raise ConflictError("Context cannot be continued; send a new explicit input")
            item, _ = self._store.enqueue(
                session,
                text="",
                request_id=request_id,
                key=request_id,
                kind="continue",
                logical_id=original.logical_id,
            )
        elif action == "compact":
            if not self._compact_enabled:
                raise ConflictError("No compaction policy registered")
            item, _ = self._store.enqueue(
                session, text="", request_id=request_id, key=request_id, kind="compact"
            )
        else:
            raise ValueError("Unknown control action")
        self.events.publish(session_id, "input_received", {"input": self._input_view(item)})
        self._kick(session_id)
        return self._receipt(item)

    async def update_session_config(
        self,
        principal: Principal,
        session_id: str,
        *,
        settings: SessionSettings,
        expected_revision: int,
    ) -> dict[str, Any]:
        session = self._authorized(principal, session_id)
        self._validate_settings(settings)
        if not principal.is_admin and (
            settings.allowed_tools != session.settings.allowed_tools
            or settings.approval_tools != session.settings.approval_tools
            or settings.enabled_extensions != session.settings.enabled_extensions
        ):
            raise AccessDenied("Only administrators may change tool/extension grants")
        revision = self._store.update_session(session_id, settings, expected_revision)
        self.events.publish(session_id, "config_changed", {})
        return {"revision": revision, "settings": settings.model_dump(mode="json")}

    async def config(self, principal: Principal) -> dict[str, Any]:
        self._require_started()
        if not principal.is_admin:
            raise AccessDenied("Administrator access required")
        revision, settings = self._store.config()
        return {
            "revision": revision,
            "settings": settings.model_dump(mode="json"),
            "models": self._catalog.choices(),
            "tools": list(self._tools),
            "extensions": [
                {
                    "id": m.id,
                    "version": m.version,
                    "scope": m.scope,
                    "capabilities": list(m.capabilities),
                }
                for m in self._extensions
            ],
        }

    async def update_config(
        self, principal: Principal, *, settings: HostSettings, expected_revision: int
    ) -> dict[str, Any]:
        await self.config(principal)
        HostSettings.model_validate_json(settings.model_dump_json())
        self._validate_settings(settings.defaults)
        self._store.update_config(settings, expected_revision)
        return await self.config(principal)

    def _admin(self, principal: Principal) -> None:
        self._require_started()
        if not principal.is_admin:
            raise AccessDenied("Administrator access required")

    async def providers(self, principal: Principal) -> dict[str, Any]:
        self._admin(principal)
        return self._catalog.providers()

    async def put_provider(
        self,
        principal: Principal,
        identity: str,
        *,
        settings: ProviderSettings,
        expected_revision: int,
        secret_action: str = "keep",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        self._admin(principal)
        return self._catalog.put_provider(
            identity,
            settings,
            expected_revision=expected_revision,
            secret_action=secret_action,
            api_key=api_key,
        )

    async def delete_provider(
        self, principal: Principal, identity: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._admin(principal)
        self._catalog.delete_provider(identity, expected_revision)
        return {"deleted": True}

    async def test_provider(
        self, principal: Principal, identity: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._admin(principal)
        return await self._catalog.test_provider(identity, expected_revision)

    async def managed_models(self, principal: Principal) -> dict[str, Any]:
        self._admin(principal)
        return self._catalog.models()

    async def put_model(
        self,
        principal: Principal,
        identity: str | None = None,
        *,
        settings: ModelSettings,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._admin(principal)
        return self._catalog.put_model(identity, settings, expected_revision)

    async def delete_model(
        self, principal: Principal, identity: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._admin(principal)
        self._catalog.delete_model(identity, expected_revision)
        return {"deleted": True}

    async def set_default_model(
        self, principal: Principal, identity: str, *, expected_revision: int
    ) -> dict[str, Any]:
        self._admin(principal)
        revision, host = self._store.config()
        if revision != expected_revision:
            raise ConflictError("Configuration revision changed")
        defaults = host.defaults.model_copy(update={"model_id": identity})
        self._validate_settings(defaults)
        self._store.update_config(host.model_copy(update={"defaults": defaults}), revision)
        return await self.config(principal)

    async def resolve_approval(
        self, principal: Principal, session_id: str, approval_id: str, *, decision: str
    ) -> dict[str, Any]:
        self._authorized(principal, session_id)
        if self._store.approval(approval_id)["session_id"] != session_id:
            raise AccessDenied("Approval is bound to another session")
        return self._approvals.resolve(approval_id, decision=decision, principal_id=principal.id)

    def _kick(self, session_id: str) -> None:
        if self._closing or session_id in self._unavailable or not self._store.pending(session_id):
            return
        current = self._tasks.get(session_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._drain(session_id), name=f"moonfox-session-{session_id}")
        self._tasks[session_id] = task

        def settled(done: asyncio.Task[None]) -> None:
            if self._tasks.get(session_id) is done:
                self._tasks.pop(session_id, None)
            if not done.cancelled() and done.exception() is not None:
                self._unavailable.add(session_id)
            if not self._closing:
                self._kick(session_id)

        task.add_done_callback(settled)

    def bind_channel(
        self, binding: ChannelBinding, *, delivery_timeout: float = 30.0
    ) -> BoundChannelGateway:
        """Trusted composition-only factory; never exposed as a model/HTTP command."""
        self._require_started()
        gateway = BoundChannelGateway(self, self._store, binding, delivery_timeout=delivery_timeout)
        self._channels.append(gateway)
        return gateway

    def channel_session(self, binding: ChannelBinding, target: str) -> SessionRecord:
        self._require_started()
        if target not in binding.targets:
            raise AccessDenied("Channel target is not authorized")
        self._validate_settings(self._store.config()[1].defaults)
        route = hashlib.sha256(
            json.dumps([binding.connection_id, binding.bot_account, target]).encode()
        ).hexdigest()
        record = self._store.create_session(
            owner_id=binding.owner_id,
            domain="qq",
            title=f"QQ · {target}"[:120],
            request_id=f"channel:{route}",
            settings=self._store.config()[1].defaults,
            route_key=route,
        )
        if record.id not in self._views:
            self._views[record.id] = self._load_view(record)
        return record

    def channel_input(self, session: SessionRecord, *, text: str, key: str) -> InputReceipt:
        self._require_started()
        receipt = self._submit(session, text=text, request_id=key, key=key)
        return InputReceipt(receipt["input_id"], session.id)

    async def channel_cancel(self, binding: ChannelBinding, target: str) -> None:
        record = self.channel_session(binding, target)
        await self.control(
            Principal(binding.owner_id), record.id, action="cancel", request_id="channel-cancel"
        )

    async def _runtime(
        self,
        session: SessionRecord,
        stream: StreamFn,
        permission: PermissionChecker,
        source: ModelRegistration,
    ) -> SessionRuntime:
        live = self._lives.get(session.id)
        if live is not None and (
            live.revision != session.config_revision or live.model_revision != source.revision
        ):
            await live.runtime.aclose()
            self._lives.pop(session.id)
            live = None
            self._views[session.id] = self._load_view(session)
        if live is None:
            ports = RunPorts()
            ports.bind(stream, permission)
            runtime = await self._factory(session, ports.stream, ports.permission, source)
            live = _LiveSession(runtime, ports, session.config_revision, source.revision)
            self._lives[session.id] = live
        else:
            live.ports.bind(stream, permission)
        return live.runtime

    async def _drain(self, session_id: str) -> None:
        while not self._closing and session_id not in self._unavailable:
            pending = self._store.pending(session_id)
            if not pending:
                return
            await self._run_input(self._store.session(session_id), pending[0])

    async def _run_input(self, session: SessionRecord, pending: InputRecord) -> None:
        actor = _Actor(
            messages={message["id"]: message for message in view_messages(self._views[session.id])}
        )
        self._actors[session.id] = actor
        item = self._store.claim(pending.id, baseline_id=self._views[session.id].leaf_id)
        actor.input_id = item.id
        settings = session.settings
        start = time.monotonic()
        status, error = "error", None
        handle: RegistrationHandle | None = None
        runtime: SessionRuntime | None = None
        self.events.publish(
            session.id, "run_started", {"input": self._input_view(item), "status": "running"}
        )

        async def permission(request: ToolPermissionRequest) -> PermissionDecision:
            if request.binding != SessionBinding(session.id, session.scope_id) or actor.cancelled:
                return PermissionDecision(False, "Cancelled or incorrect scope")
            if request.tool_name not in settings.allowed_tools:
                return PermissionDecision(False, "Tool not allowed by host")
            if request.tool_name in settings.approval_tools:
                return await self._approvals.request(request, item.id)
            return PermissionDecision(True)

        def on_event(event: SessionEvent) -> None:
            if runtime is not None and event.type in (
                "entry_committed",
                "entries_committed",
                "agent_settled",
            ):
                self._views[session.id] = runtime.view()
            update = session_update(event)
            if update:
                kind, payload = update
                if kind in ("message_start", "message_end"):
                    actor.messages[payload["message"]["id"]] = deepcopy(payload["message"])
                elif kind == "text_delta" and payload["message_id"] in actor.messages:
                    actor.messages[payload["message_id"]]["text"] += payload["delta"]
                self.events.publish(session.id, kind, payload)

        try:
            budget = self._store.budget(item.logical_id)
            remaining_ms = budget.max_milliseconds - budget.milliseconds_used
            remaining_requests = budget.max_requests - budget.requests_used
            if remaining_ms <= 0 or remaining_requests <= 0:
                raise BudgetExceeded("Logical input budget exhausted")
            async with asyncio.timeout(remaining_ms / 1000):
                source = self._catalog.resolve(settings)
                runtime = await self._runtime(
                    session,
                    self._gateway.bind(item.logical_id, source.stream_fn, defaults=source.options),
                    permission,
                    source,
                )
                actor.runtime = runtime
                handle = runtime.subscribe(on_event)
                self._views[session.id] = runtime.view()
                if actor.cancelled or self._closing:
                    status = "aborted"
                elif item.kind == "compact":
                    await runtime.compact()
                    status = "completed"
                else:
                    options = RunOptions(min(settings.max_turns_per_run, remaining_requests))
                    outcome = (
                        await runtime.prompt(item.text, options=options)
                        if item.kind == "prompt"
                        else await runtime.continue_(options=options)
                    )
                    status = outcome.reason
        except InvalidConfiguration:
            status, error = "error", "提供商或模型配置不可用，请检查启用状态、凭据与生成参数。"
        except BudgetExceeded:
            status, error = "limit_reached", "Logical input budget exhausted"
        except TimeoutError:
            status, error = "aborted", "Logical input time budget exhausted"
        except asyncio.CancelledError:
            status, error = "aborted", "Host operation cancelled"
        except Exception:
            status, error = "error", "Run failed; inspect host diagnostics"
        finally:
            if handle is not None:
                handle.dispose()
            if runtime is not None:
                try:
                    self._views[session.id] = runtime.view()
                    await runtime.wait_for_idle()
                except Exception:
                    status, error = "unknown", "Session cleanup failed; not replayed"
                    self._unavailable.add(session.id)
            live = self._lives.get(session.id)
            if live is not None:
                live.ports.revoke()
            self._approvals.cancel_session(session.id)
            self._store.charge_time(item.logical_id, round((time.monotonic() - start) * 1000))
            reply = None
            if status == "completed" and session.domain == "qq" and session.route_key:
                messages = view_messages(self._views[session.id])
                if messages and messages[-1]["role"] == "assistant" and messages[-1]["text"]:
                    reply = (session.route_key, messages[-1]["text"])
            self._store.finish(item.id, status=status, error=error, reply=reply)
            self._actors.pop(session.id, None)
            self.events.publish(
                session.id,
                "run_finished",
                {"input": self._input_view(self._store.input(item.id)), "status": status},
            )
            for channel in self._channels:
                channel.wake()

    async def wait_for_idle(self, session_id: str | None = None) -> None:
        while True:
            tasks = [
                task
                for identity, task in self._tasks.items()
                if session_id is None or identity == session_id
            ]
            if not tasks:
                return
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
            await asyncio.sleep(0)

    async def _close(self) -> None:
        for session_id, actor in tuple(self._actors.items()):
            actor.cancelled = True
            if actor.runtime:
                actor.runtime.abort()
            self._approvals.cancel_session(session_id)
        await self.wait_for_idle()
        for channel in self._channels:
            await channel.aclose()
        failures: list[Exception] = []
        for live in self._lives.values():
            try:
                await live.runtime.aclose()
            except Exception as exc:
                failures.append(exc)
        self._lives.clear()
        self.events.close()
        self._store.close()
        self._closed = True
        if failures:
            raise ExceptionGroup("Session shutdown failed", failures)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="moonfox-host-close")
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
        if cancelled:
            raise cancelled
