"""Single-session persistence around the public Agent hooks, without host/service imports."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from moonfox_agent.api import (
    AbortToken,
    Agent,
    AgentContext,
    AgentEnd,
    AgentEvent,
    AgentInitialState,
    AgentRequestUpdate,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    MessageEnd,
    PrepareRequestContext,
    RunOptions,
    RunOutcome,
    TurnContext,
    TurnDecision,
)
from moonfox_ai.api import (
    AbortSignal,
    AssistantMessage,
    Model,
    StreamFn,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from moonfox_app.extension_api import (
    BoundaryPreview,
    ContextBlock,
    CustomEntryDraft,
    Delivery,
    EventHandler,
    ExtensionDiagnostic,
    PermissionChecker,
    PermissionDecision,
    RegistrationHandle,
    SessionEvent,
    SessionExtensionPort,
    SessionView,
    ToolPermissionRequest,
)

from ..compaction import (
    CompactionPolicy,
    CompactionRequest,
    CompactionResult,
    CompactionSettings,
    context_tokens,
)
from ..prompt import PromptPipelineOptions, prompt_delta, request_messages
from .entries import CompactionDraft, EntryDraft, MessageDraft, ModelChangeDraft, ThinkingLevelDraft
from .events import SessionNotifications
from .manager import SessionManager
from .projection import SessionProjection

_IN_SESSION: ContextVar[object | None] = ContextVar("moonfox_session_operation", default=None)


@dataclass(slots=True)
class _Operation:
    signal: AbortToken
    done: asyncio.Future[None]


class AgentSession:
    """One owned operation at a time. Persistence is authoritative, Agent is a run mechanism.

    Application queues here are volatile core steering/follow-up queues, not the durable
    inbox planned for services. Closing drops unconsumed queue work; no auto-run or retry.
    """

    def __init__(
        self,
        manager: SessionManager,
        *,
        stream_fn: StreamFn,
        tools: Sequence[AgentTool] = (),
        permission_checker: PermissionChecker | None = None,
        prompt_options: PromptPipelineOptions | None = None,
        compaction_policy: CompactionPolicy | None = None,
        compaction_settings: CompactionSettings | None = None,
        notification_timeout: float = 5.0,
        permission_timeout: float = 5.0,
    ) -> None:
        if permission_timeout <= 0:
            raise ValueError("Permission timeout must be positive")
        self._manager = manager
        self._tools = tuple(tools)
        self._permission = permission_checker
        self._permission_timeout = permission_timeout
        self._prompt_options = prompt_options or PromptPipelineOptions()
        self._compactor = compaction_policy
        self._compaction = compaction_settings or CompactionSettings()
        self._extensions: SessionExtensionPort | None = None
        self._notifications = SessionNotifications(timeout=notification_timeout)
        self._active: _Operation | None = None
        self._closing = False
        self._closed = False
        self._settling = False
        self._boundary_active = False
        self._staged: list[CustomEntryDraft] = []
        self._compactions_used = 0
        self._last_outcome: RunOutcome | None = None
        self.recovered_tool_calls = manager.recover_pending_tools()
        projection = manager.projection()
        self._agent = Agent(
            initial_state=AgentInitialState(
                projection.model,
                messages=list(projection.messages),
                thinking_level=projection.thinking_level,
            ),
            stream_fn=stream_fn,
            prepare_request=self._prepare_request,
            finish_turn=self._finish_turn,
            before_tool_call=self._before_tool_call,
        )
        self._agent.subscribe(self._on_agent_event)
        self._sync_agent()

    def ensure_idle(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("Session is closed or closing")
        if self._active is not None:
            raise RuntimeError("Session is busy; wait_for_idle before changing state")

    @property
    def is_running(self) -> bool:
        return self._active is not None

    @property
    def diagnostics(self) -> tuple[ExtensionDiagnostic, ...]:
        return tuple(self._notifications.diagnostics)

    def view(self) -> SessionView:
        return self._view(self._manager.projection(allow_pending=True))

    def _view(self, projection: SessionProjection) -> SessionView:
        return SessionView(
            projection.binding,
            self._manager.snapshot().revision,
            projection.leaf_id,
            tuple(deepcopy(projection.messages)),
            projection.model,
            projection.thinking_level,
        )

    def subscribe(self, listener: EventHandler) -> RegistrationHandle:
        if self._closed:
            raise RuntimeError("Session is closed")
        return self._notifications.subscribe(listener)

    def bind_extensions(self, port: SessionExtensionPort) -> None:
        self.ensure_idle()
        if self._extensions is not None:
            raise RuntimeError("An extension port is already bound")
        self._extensions = port

    def _all_tools(self) -> list[AgentTool]:
        tools = [*self._tools, *(self._extensions.tools() if self._extensions else ())]
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Application and extension tool names conflict")
        return tools

    def _sync_agent(self) -> None:
        projection = self._manager.projection()
        self._agent.replace_messages(list(projection.messages))
        self._agent.set_model(projection.model)
        self._agent.set_thinking_level(projection.thinking_level)
        self._agent.set_tools(self._all_tools())

    async def _emit(self, event_type: str, payload: Any = None) -> None:
        event = SessionEvent(
            event_type, self._manager.binding, self._manager.snapshot().revision, deepcopy(payload)
        )
        if self._extensions is not None:
            await self._extensions.notify(deepcopy(event))
        await self._notifications.emit(event)

    async def _on_agent_event(self, event: AgentEvent, signal: AbortToken) -> None:
        if isinstance(event, MessageEnd):
            if not isinstance(
                event.message, (SystemMessage, UserMessage, AssistantMessage, ToolResultMessage)
            ):
                raise TypeError("Persistent sessions require an explicit custom entry codec")
            self._manager.commit(
                [MessageDraft(event.message, event.message_id)], allow_pending=True
            )
            await self._emit("entry_committed", {"entry_ids": (event.message_id,)})
        elif isinstance(event, AgentEnd):
            self._last_outcome = event.outcome
        await self._emit("agent_event", event)

    def stage_entries(self, drafts: Sequence[CustomEntryDraft]) -> None:
        if self._closed or self._closing or self._settling or self._boundary_active:
            raise RuntimeError("Session is not accepting staged entries")
        if any(not isinstance(draft, CustomEntryDraft) for draft in drafts):
            raise TypeError("Extensions can only stage CustomEntryDraft")
        # Nothing is written here. Validation happens on detached candidate data.
        proposed = deepcopy([*self._staged, *drafts])
        self._manager.preview(proposed, allow_pending=True)
        self._staged = proposed

    def enqueue(self, message: UserMessage, delivery: Delivery) -> None:
        if self._closed or self._closing or self._settling or self._boundary_active:
            raise RuntimeError("Session is not accepting queued messages")
        if not isinstance(message, UserMessage):
            raise TypeError("Only user messages can be enqueued")
        if delivery == "steer":
            self._agent.steer(message)
        elif delivery == "follow_up":
            self._agent.follow_up(message)
        else:
            raise ValueError("Unknown delivery mode")

    def _begin(self) -> tuple[_Operation, Token[object | None]]:
        self.ensure_idle()
        active = _Operation(AbortToken(), asyncio.get_running_loop().create_future())
        self._active = active
        self._last_outcome = None
        self._compactions_used = 0
        return active, _IN_SESSION.set(active)

    def _end(self, active: _Operation, marker: Token[object | None]) -> None:
        self._active = None
        self._settling = False
        _IN_SESSION.reset(marker)
        active.done.set_result(None)

    async def _sync_contributions(self) -> None:
        sections = await self._extensions.prompt_sections(self.view()) if self._extensions else ()
        projection = self._manager.projection()
        delta = prompt_delta(projection.messages, sections, self._all_tools())
        staged, self._staged = self._staged, []
        drafts: list[EntryDraft] = list(staged)
        if delta is not None:
            drafts.append(MessageDraft(delta))
        if drafts:
            preview = self._manager.preview(drafts)
            self._manager.commit_preview(preview)
            await self._emit(
                "entries_committed", {"entry_ids": tuple(e.id for e in preview.entries)}
            )

    async def refresh_extensions(self) -> None:
        """Safe-boundary declaration/section synchronization after startup or unloading."""
        active, marker = self._begin()
        try:
            await self._sync_contributions()
            self._sync_agent()
        finally:
            self._end(active, marker)

    async def _prepare_request(
        self, context: PrepareRequestContext, signal: AbortSignal | None
    ) -> AgentRequestUpdate:
        assert self._active is not None
        if not self._active.signal.aborted and not (signal and signal.aborted):
            await self._sync_contributions()
            threshold = self._compaction.trigger_tokens
            projection = self._manager.projection()
            if (
                threshold is not None
                and self._compactor is not None
                and self._compactions_used < self._compaction.max_per_run
                and context_tokens(projection.messages) > threshold
            ):
                self._compactions_used += 1
                await self._compact("threshold")
        projection = self._manager.projection()
        blocks: tuple[ContextBlock, ...] = ()
        if (
            self._extensions is not None
            and not self._active.signal.aborted
            and not (signal and signal.aborted)
        ):
            blocks = await self._extensions.context_blocks(self._view(projection))
        messages = request_messages(projection.messages, blocks, self._prompt_options)
        return AgentRequestUpdate(
            context=AgentContext(list(messages), self._all_tools()),
            model=projection.model,
            thinking_level=projection.thinking_level,
        )

    async def _before_tool_call(
        self, context: BeforeToolCallContext, signal: AbortSignal | None
    ) -> BeforeToolCallResult:
        if signal and signal.aborted:
            return BeforeToolCallResult(True, "Cancelled")
        if not isinstance(context.args, BaseModel):
            return BeforeToolCallResult(True, "Invalid validated tool arguments")
        request = ToolPermissionRequest(
            self._manager.binding,
            context.tool_call.name,
            context.tool_call.id,
            context.args.model_dump(mode="python"),
            context.parent_call_id,
            context.depth,
        )
        if self._permission is None:
            return BeforeToolCallResult(True, "No host permission checker was configured")
        try:
            async with asyncio.timeout(self._permission_timeout):
                decision = await self._permission(deepcopy(request))
            if not isinstance(decision, PermissionDecision) or type(decision.allowed) is not bool:
                raise TypeError("Invalid host permission decision")
        except Exception as exc:
            self._notifications.diagnostics.append(
                ExtensionDiagnostic("<host>", "tool_permission", str(exc))
            )
            return BeforeToolCallResult(True, "Host permission check failed")
        if not decision.allowed:
            return BeforeToolCallResult(True, decision.reason or "Denied by host")
        if self._extensions is not None:
            decision = await self._extensions.check_tool(request)
            if not isinstance(decision, PermissionDecision) or decision.allowed is not True:
                return BeforeToolCallResult(True, "Denied by extension tool check")
        return BeforeToolCallResult()

    async def _finish_turn(
        self, context: TurnContext, signal: AbortSignal | None
    ) -> TurnDecision | None:
        staged, self._staged = tuple(self._staged), []
        outcome = RunOutcome(
            "aborted"
            if (signal and signal.aborted) or context.message.stop_reason == "aborted"
            else "error"
            if context.message.stop_reason == "error"
            else "completed",
            context.turn,
        )

        def validate(drafts: tuple[CustomEntryDraft, ...]) -> BoundaryPreview:
            if any(not isinstance(draft, CustomEntryDraft) for draft in drafts):
                raise TypeError("Extension boundaries can only add custom entries")
            preview = self._manager.preview(drafts)
            return BoundaryPreview(self._view(preview.projection), deepcopy(drafts), outcome)

        preview = validate(staged)
        should_continue = False
        if self._extensions is not None and outcome.reason == "completed":
            self._boundary_active = True
            try:
                result = await self._extensions.boundary(preview, validate)
                validate(result.drafts)
                if type(result.continue_) is not bool:
                    raise TypeError("Boundary continuation must be bool")
                staged, should_continue = result.drafts, result.continue_
            finally:
                self._boundary_active = False
        if staged:
            self._manager.commit(staged)
            await self._emit("boundary_committed")
        return "continue" if should_continue and outcome.reason == "completed" else None

    async def prompt(self, message: str | UserMessage, *, options: RunOptions) -> RunOutcome:
        if not isinstance(message, (str, UserMessage)):
            raise TypeError("Session prompt requires text or a user message")
        return await self._run(message, options)

    async def continue_(self, *, options: RunOptions) -> RunOutcome:
        return await self._run(None, options)

    async def _run(self, message: str | UserMessage | None, options: RunOptions) -> RunOutcome:
        if not isinstance(options, RunOptions):
            raise TypeError("Every run requires RunOptions")
        active, marker = self._begin()
        outcome = RunOutcome("error", 0)
        failure: BaseException | None = None
        try:
            self._sync_agent()
            if message is None:
                outcome = await self._agent.continue_(options=options)
            else:
                outcome = await self._agent.prompt(message, options=options)
        except (Exception, asyncio.CancelledError) as exc:
            failure = exc
            outcome = self._last_outcome or RunOutcome(
                "aborted" if isinstance(exc, asyncio.CancelledError) else "error", 0
            )
        finally:
            self._settling = True
            # A caller cancellation must not tear down persistence/notifications midway.
            task = asyncio.create_task(self._settle(outcome), name="moonfox-session-settle")
            try:
                while True:
                    try:
                        await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as exc:
                        failure = failure or exc
                        active.signal.abort()
                        if task.done():
                            try:
                                task.result()
                            except (Exception, asyncio.CancelledError) as settle_error:
                                failure = failure or settle_error
                            break
                    except Exception as exc:
                        failure = failure or exc
                        break
            finally:
                self._end(active, marker)
        if failure is not None:
            raise failure
        return outcome

    async def _settle(self, outcome: RunOutcome) -> None:
        if self._staged:
            staged, self._staged = self._staged, []
            self._manager.commit(staged)
        self._sync_agent()
        await self._emit("agent_settled", outcome)

    async def _compact(
        self, reason: Literal["manual", "threshold"], instructions: str | None = None
    ) -> CompactionResult | None:
        assert self._active is not None
        if self._compactor is None:
            raise RuntimeError("No compaction policy was configured")
        before = self._manager.projection()
        revision = self._manager.snapshot().revision
        await self._emit("compaction_start", {"reason": reason})
        result: CompactionResult | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(self._compaction.timeout):
                draft = await self._compactor(
                    CompactionRequest(deepcopy(before), reason, instructions), self._active.signal
                )
            if draft is None or self._active.signal.aborted:
                return None
            if not isinstance(draft, CompactionDraft):
                raise TypeError("Compaction policy must return CompactionDraft or None")
            if self._manager.snapshot().revision != revision:
                raise RuntimeError("Session changed while summarizing")
            preview = self._manager.preview([draft])
            tokens_before = context_tokens(before.messages)
            tokens_after = context_tokens(preview.projection.messages)
            if tokens_after >= tokens_before:
                raise ValueError("Compaction did not reduce estimated context size")
            self._manager.commit_preview(preview)
            result = CompactionResult(
                preview.entries[-1].id,
                draft.summary,
                tokens_before,
                tokens_after,
                deepcopy(draft.usage),
            )
            return result
        except (Exception, asyncio.CancelledError) as exc:
            error = str(exc) or "Cancelled"
            raise
        finally:
            await self._emit("compaction_end", {"reason": reason, "result": result, "error": error})

    async def compact(self, *, instructions: str | None = None) -> CompactionResult | None:
        active, marker = self._begin()
        try:
            result = await self._compact("manual", instructions)
            self._sync_agent()
            return result
        finally:
            self._end(active, marker)

    def set_model(self, model: Model) -> None:
        self.ensure_idle()
        self._manager.commit([ModelChangeDraft(model)])
        self._sync_agent()

    def set_thinking_level(self, level: str) -> None:
        self.ensure_idle()
        self._manager.commit([ThinkingLevelDraft(level)])
        self._sync_agent()

    def select_leaf(self, leaf_id: str) -> None:
        self.ensure_idle()
        if self._staged or self._agent.has_queued_messages():
            raise RuntimeError("Cannot change branch with pending input or drafts")
        self._manager.select_leaf(leaf_id)
        self._sync_agent()

    def abort(self) -> None:
        if self._active is not None:
            self._active.signal.abort()
            self._agent.abort()

    def ensure_external_operation(self) -> None:
        active = self._active
        if active is not None and _IN_SESSION.get() is active:
            raise RuntimeError("Session callbacks cannot close their own operation")

    async def wait_for_idle(self) -> None:
        active = self._active
        if active is not None:
            if _IN_SESSION.get() is active:
                raise RuntimeError("Session callbacks cannot wait for their own operation")
            await asyncio.shield(active.done)

    async def aclose(self) -> None:
        if self._closed:
            return
        active = self._active
        if active is not None and _IN_SESSION.get() is active:
            raise RuntimeError("Session callbacks cannot close their own operation")
        self._closing = True
        self.abort()
        await self.wait_for_idle()
        self._agent.clear_all_queues()
        self._staged.clear()
        self._notifications.clear()
        self._manager.close()
        self._closed = True
