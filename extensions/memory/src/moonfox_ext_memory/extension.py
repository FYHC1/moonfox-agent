"""Session lifecycle, context, tools, and owned extraction worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from typing import Any

from moonfox_app.extension_api import (
    AbortSignal,
    AgentTool,
    AgentToolResult,
    ContextBlock,
    ExtensionConflictError,
    ExtensionRecordNotFoundError,
    ExtensionResources,
    ModelRequestRejectedError,
    RegistrationHandle,
    RunOutcome,
    SessionEvent,
    SessionExtensionAPI,
    SessionExtensionContext,
    SessionView,
    TextContent,
    ToolUpdate,
)

from .extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTOR_VERSION,
    parse_candidates,
)
from .retrieval import RETRIEVAL_VERSION, build_context, latest_user_input
from .settings import (
    ForgetToolArguments,
    GetToolArguments,
    MemorySettings,
    RememberToolArguments,
    SearchToolArguments,
)
from .storage import open_scoped_store
from .store import ScopedMemoryStore

_CACHE_LIMIT = 32
_EXTRACTION_TIMEOUT_SECONDS = 120.0
_ATTEMPTED_SOURCE_LIMIT = 128
_MAX_EXTRACTION_TASKS = 4


class MemoryExtension:
    """One configured extension instance bound to exactly one host scope."""

    def __init__(self, resources: ExtensionResources, settings: MemorySettings) -> None:
        self._resources = resources
        self.settings = settings
        self._store: ScopedMemoryStore | None = None
        self._context: SessionExtensionContext | None = None
        self._handles: list[RegistrationHandle] = []
        self._submitted_jobs: set[str] = set()
        self._attempted_sources: OrderedDict[str, None] = OrderedDict()
        self._cache: OrderedDict[tuple[object, ...], tuple[ContextBlock, ...]] = OrderedDict()
        self._setup_complete = False
        self._disposed = False
        self._stopping = False

    async def setup(self, api: SessionExtensionAPI) -> None:
        if self._disposed:
            raise RuntimeError("Memory extension is disposed")
        if self._setup_complete:
            return
        handles: list[RegistrationHandle] = []
        try:
            handles.append(api.on("agent_settled", self._on_event))
            handles.append(api.register_context_provider(self._provide_context))
            handles.append(api.register_tool(self._search_tool()))
            handles.append(api.register_tool(self._get_tool()))
            handles.append(api.register_tool(self._remember_tool()))
            handles.append(api.register_tool(self._forget_tool()))
        except BaseException:
            for handle in reversed(handles):
                try:
                    handle.dispose()
                except BaseException:
                    pass
            raise
        self._handles.extend(handles)
        self._setup_complete = True

    def _search_tool(self) -> AgentTool:
        return AgentTool(
            name="memory.search",
            description="Search active, scope-bound memory by literal keywords.",
            label="Search memory",
            parameters=SearchToolArguments,
            execute=self._execute_search,
        )

    def _get_tool(self) -> AgentTool:
        return AgentTool(
            name="memory.get",
            description="Read one memory record by id in this session scope.",
            label="Get memory",
            parameters=GetToolArguments,
            execute=self._execute_get,
        )

    def _remember_tool(self) -> AgentTool:
        return AgentTool(
            name="memory.remember",
            description="Remember a concise user-provided item in this session scope.",
            label="Remember memory",
            parameters=RememberToolArguments,
            execute=self._execute_remember,
        )

    def _forget_tool(self) -> AgentTool:
        return AgentTool(
            name="memory.forget",
            description="Stop recalling a memory record using its current revision.",
            label="Forget memory",
            parameters=ForgetToolArguments,
            execute=self._execute_forget,
        )

    def _active_state(
        self,
    ) -> tuple[ExtensionResources, SessionExtensionContext, ScopedMemoryStore]:
        resources = self._resources
        context = self._context
        store = self._store
        if (
            self._disposed
            or self._stopping
            or context is None
            or store is None
            or store.closed
            or context.binding != resources.binding
        ):
            raise RuntimeError("Memory extension is not active for this binding")
        return resources, context, store

    @staticmethod
    def _binding_matches(view: SessionView, resources: ExtensionResources) -> bool:
        return view.binding == resources.binding

    def _current_view(
        self, resources: ExtensionResources, context: SessionExtensionContext
    ) -> SessionView:
        view = context.actions.view()
        if not self._binding_matches(view, resources):
            raise RuntimeError("Current view does not match the attached memory scope")
        return view

    async def attach(self, ctx: SessionExtensionContext) -> None:
        if self._disposed:
            raise RuntimeError("Memory extension is disposed")
        if self._store is not None:
            raise RuntimeError("Memory extension is already attached")
        if ctx.binding != self._resources.binding:
            raise ValueError("Session binding does not match memory resources")
        self._stopping = False
        store = open_scoped_store(self._resources)
        self._context = ctx
        self._store = store
        try:
            store.recover_running_jobs()
            self._resume_pending()
        except BaseException:
            self._context = None
            self._store = None
            self._cache.clear()
            store.close()
            raise

    def _resume_pending(self) -> None:
        models = self._resources.models
        if self.settings.extraction != "model" or models is None:
            return
        try:
            resources, context, store = self._active_state()
            self._current_view(resources, context)  # Check the bound/revocable session capability.
            available = _MAX_EXTRACTION_TASKS - len(self._submitted_jobs)
            if available <= 0:
                return
            for pending in store.pending_extractions(EXTRACTOR_VERSION, limit=available):
                if pending.budget_ticket is None:
                    # Legacy/unbound work must not borrow today's logical input budget.
                    if store.claim_job(pending.id, pending.capture_epoch) is not None:
                        store.mark_job_failed(pending.id, pending.capture_epoch)
                    continue
                self._submit_job(pending.id, pending.capture_epoch, pending.budget_ticket)
        except Exception:
            return

    async def detach(self) -> None:
        self._stopping = True
        context, store = self._context, self._store
        try:
            if context is not None:
                await context.tasks.cancel_all()
        finally:
            if store is not None:
                store.close()
            self._context = None
            self._store = None
            self._submitted_jobs.clear()
            self._attempted_sources.clear()
            self._cache.clear()

    async def dispose(self) -> None:
        if self._disposed:
            return
        failure: BaseException | None = None
        if self._store is not None:
            try:
                await self.detach()
            except BaseException as exc:
                failure = exc
        for handle in reversed(self._handles):
            try:
                handle.dispose()
            except BaseException as exc:
                failure = failure or exc
        self._handles.clear()
        self._cache.clear()
        self._submitted_jobs.clear()
        self._attempted_sources.clear()
        self._disposed = True
        if failure is not None:
            raise failure

    async def _provide_context(self, view: SessionView) -> tuple[ContextBlock, ...]:
        if not self.settings.recall:
            return ()
        try:
            resources, context, store = self._active_state()
            if not self._binding_matches(view, resources):
                return ()
            current_view = self._current_view(resources, context)
            if not self._binding_matches(current_view, resources):
                return ()
            latest = latest_user_input(view)
            if latest is None:
                return ()
            message_id, query = latest
            bounded_query = query[:400]
            store_revision = store.scope_revision()
            key = self._cache_key(view, resources, message_id, store_revision)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            search_revision, records = store.search_with_revision(
                bounded_query, self.settings.max_results
            )
            if search_revision != store_revision:
                key = self._cache_key(view, resources, message_id, search_revision)
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
            if not records:
                return self._cache_put(key, ())
            selected, text = build_context(records, self.settings.max_reference_tokens)
            if text is None:
                return self._cache_put(key, ())
            snapshot_id = hashlib.sha256(
                "\0".join(
                    (
                        resources.binding.session_id,
                        resources.binding.scope_id,
                        resources.domain,
                        message_id,
                        str(view.revision),
                        str(search_revision),
                        RETRIEVAL_VERSION,
                    )
                ).encode("utf-8")
            ).hexdigest()
            saved = store.save_snapshot(
                snapshot_id,
                message_id,
                view.revision,
                search_revision,
                tuple((record.id, record.revision) for record in selected),
                text,
            )
            if not saved:
                return ()
            return self._cache_put(key, (ContextBlock(source="memory", text=text),))
        except Exception:
            return ()

    @staticmethod
    def _cache_key(
        view: SessionView,
        resources: ExtensionResources,
        message_id: str,
        store_revision: int,
    ) -> tuple[object, ...]:
        return (
            view.binding,
            resources.domain,
            resources.binding.scope_id,
            message_id,
            view.revision,
            store_revision,
            RETRIEVAL_VERSION,
        )

    def _cache_put(
        self, key: tuple[object, ...], blocks: tuple[ContextBlock, ...]
    ) -> tuple[ContextBlock, ...]:
        self._cache[key] = blocks
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_LIMIT:
            self._cache.popitem(last=False)
        return blocks

    async def _execute_search(
        self,
        call_id: str,
        arguments: Any,
        signal: AbortSignal | None,
        update: ToolUpdate | None,
    ) -> AgentToolResult:
        del call_id, signal, update
        try:
            parsed = (
                arguments
                if isinstance(arguments, SearchToolArguments)
                else SearchToolArguments.model_validate(arguments)
            )
            resources, context, store = self._active_state()
            self._current_view(resources, context)
            records = store.search(
                parsed.query,
                min(parsed.limit, self.settings.max_results),
            )
            return self._tool_result({"records": [record.to_dict() for record in records]})
        except Exception as exc:
            return self._tool_error(exc)

    async def _execute_get(
        self,
        call_id: str,
        arguments: Any,
        signal: AbortSignal | None,
        update: ToolUpdate | None,
    ) -> AgentToolResult:
        del call_id, signal, update
        try:
            parsed = (
                arguments
                if isinstance(arguments, GetToolArguments)
                else GetToolArguments.model_validate(arguments)
            )
            resources, context, store = self._active_state()
            self._current_view(resources, context)
            record = store.get(parsed.id)
            if not record.active:
                raise ExtensionRecordNotFoundError("Memory is not available for model recall")
            return self._tool_result({"record": record.to_dict()})
        except Exception as exc:
            return self._tool_error(exc)

    async def _execute_remember(
        self,
        call_id: str,
        arguments: Any,
        signal: AbortSignal | None,
        update: ToolUpdate | None,
    ) -> AgentToolResult:
        del call_id, signal, update
        try:
            parsed = (
                arguments
                if isinstance(arguments, RememberToolArguments)
                else RememberToolArguments.model_validate(arguments)
            )
            resources, context, store = self._active_state()
            view = self._current_view(resources, context)
            latest = latest_user_input(view)
            if latest is None:
                return self._tool_error_message("No real user entry is available as a source.")
            entry_id, _ = latest
            record = store.remember(
                parsed.text,
                parsed.kind,
                parsed.subject,
                origin="tool",
                entry_id=entry_id,
                confidence=0.75,
            )
            return self._tool_result({"record": record.to_dict()})
        except Exception as exc:
            return self._tool_error(exc)

    async def _execute_forget(
        self,
        call_id: str,
        arguments: Any,
        signal: AbortSignal | None,
        update: ToolUpdate | None,
    ) -> AgentToolResult:
        del call_id, signal, update
        try:
            parsed = (
                arguments
                if isinstance(arguments, ForgetToolArguments)
                else ForgetToolArguments.model_validate(arguments)
            )
            resources, context, store = self._active_state()
            self._current_view(resources, context)
            record = store.forget(parsed.id, parsed.expected_revision)
            return self._tool_result(
                {"record": {"id": record.id, "revision": record.revision, "active": False}}
            )
        except Exception as exc:
            return self._tool_error(exc)

    @staticmethod
    def _tool_result(value: dict[str, object]) -> AgentToolResult:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return AgentToolResult(
            content=[TextContent(text=text)],
            structured_content=value,
        )

    @classmethod
    def _tool_error(cls, error: Exception) -> AgentToolResult:
        if isinstance(error, ExtensionConflictError):
            message = "Memory changed; refresh the record and use its current revision."
        elif isinstance(error, ExtensionRecordNotFoundError):
            message = "No memory record with that id exists in this scope."
        else:
            message = "Memory operation failed or the extension is not active."
        return cls._tool_error_message(message)

    @staticmethod
    def _tool_error_message(message: str) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=message)], is_error=True)

    async def _on_event(self, event: SessionEvent) -> None:
        if event.type != "agent_settled" or event.binding != self._resources.binding:
            return
        models = self._resources.models
        if self.settings.extraction != "model" or models is None:
            return
        if not isinstance(event.payload, RunOutcome) or event.payload.reason != "completed":
            return
        try:
            resources, context, store = self._active_state()
            view = self._current_view(resources, context)
            latest = latest_user_input(view)
            if latest is None:
                return
            entry_id, user_text = latest
            if store.find_job_for_entry(entry_id, EXTRACTOR_VERSION) is not None:
                return
            if entry_id in self._attempted_sources:
                self._attempted_sources.move_to_end(entry_id)
                return
            self._attempted_sources[entry_id] = None
            while len(self._attempted_sources) > _ATTEMPTED_SOURCE_LIMIT:
                self._attempted_sources.popitem(last=False)
            ticket = models.current_ticket()
            if not isinstance(ticket, str) or not ticket.strip():
                return
            _, created = store.queue_extraction(
                entry_id,
                user_text,
                EXTRACTOR_VERSION,
                budget_ticket=ticket,
            )
            if not created:
                return
            self._resume_pending()
        except Exception:
            return

    def _submit_job(self, job_id: str, capture_epoch: int, ticket: str) -> None:
        if job_id in self._submitted_jobs:
            return
        context = self._context
        resources = self._resources
        if context is None or resources.models is None or self._store is None:
            return
        self._submitted_jobs.add(job_id)

        async def work() -> None:
            try:
                await self._run_extraction(job_id, capture_epoch, ticket)
            finally:
                self._submitted_jobs.discard(job_id)
                self._resume_pending()

        try:
            context.tasks.submit(
                f"memory-extraction:{job_id}",
                work,
                timeout=_EXTRACTION_TIMEOUT_SECONDS,
            )
        except Exception:
            self._submitted_jobs.discard(job_id)

    async def _run_extraction(self, job_id: str, capture_epoch: int, ticket: str) -> None:
        store = self._store
        resources = self._resources
        if store is None or resources.models is None or store.closed:
            return
        work = store.claim_job(job_id, capture_epoch)
        if work is None:
            return
        if work.budget_ticket != ticket:
            store.mark_job_failed(job_id, capture_epoch)
            return
        try:
            response = await resources.models.complete(
                ticket,
                system=EXTRACTION_SYSTEM_PROMPT,
                text=work.payload,
                max_tokens=self.settings.extraction_max_tokens,
            )
        except asyncio.CancelledError:
            store.mark_job_unknown(job_id, capture_epoch)
            raise
        except ModelRequestRejectedError:
            store.mark_job_failed(job_id, capture_epoch)
            return
        except Exception:
            store.mark_job_unknown(job_id, capture_epoch)
            return
        try:
            candidates = parse_candidates(response)
        except Exception:
            store.mark_job_failed(job_id, capture_epoch)
            return
        try:
            store.complete_job(job_id, capture_epoch, candidates)
        except Exception:
            store.mark_job_failed(job_id, capture_epoch)
