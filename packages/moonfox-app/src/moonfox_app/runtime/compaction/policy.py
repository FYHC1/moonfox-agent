"""Pure safe-cut selection and a host-injected (optionally metered) summary callback."""

from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Protocol

from moonfox_agent.api import validate_tool_pairing
from moonfox_ai.api import (
    AbortSignal,
    Message,
    SystemMessage,
    Usage,
    collapse_system_messages,
    estimate_message_tokens,
)

from ..session.entries import CompactionDraft
from ..session.projection import SessionProjection


def context_tokens(messages: Sequence[Message]) -> int:
    # Old response usage describes the old prompt, not a newly compressed projection.
    return sum(estimate_message_tokens(message) for message in collapse_system_messages(messages))


@dataclass(frozen=True, slots=True)
class CompactionSettings:
    trigger_tokens: int | None = None
    max_per_run: int = 1
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.trigger_tokens is not None and (
            type(self.trigger_tokens) is not int or self.trigger_tokens < 1
        ):
            raise ValueError("trigger_tokens must be positive or None")
        if type(self.max_per_run) is not int or self.max_per_run < 0 or self.timeout <= 0:
            raise ValueError("Invalid compaction limit or timeout")


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    projection: SessionProjection
    reason: Literal["manual", "threshold"]
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    entry_id: str
    summary: str
    tokens_before: int
    tokens_after: int
    usage: Usage | None = None


class CompactionPolicy(Protocol):
    async def __call__(
        self, request: CompactionRequest, signal: AbortSignal
    ) -> CompactionDraft | None: ...


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    messages: tuple[Message, ...]
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryResult:
    summary: str
    usage: Usage | None = None


type Summarizer = Callable[[SummaryRequest, AbortSignal], Awaitable[SummaryResult]]


class SummarizingCompactor:
    """Keep a token-sized suffix, moving the cut backwards to preserve tool batches.

    The callback may correct obsolete facts and replace an earlier summary. It is
    responsible for its own model/cost policy; runtime calls it at most max_per_run.
    """

    def __init__(self, summarize: Summarizer, *, keep_recent_tokens: int) -> None:
        if type(keep_recent_tokens) is not int or keep_recent_tokens < 0:
            raise ValueError("keep_recent_tokens must be nonnegative")
        self._summarize = summarize
        self._keep = keep_recent_tokens

    async def __call__(
        self, request: CompactionRequest, signal: AbortSignal
    ) -> CompactionDraft | None:
        projection = request.projection
        pairs = [
            (identity, message)
            for identity, message in zip(
                projection.message_entry_ids, projection.messages, strict=True
            )
            if not isinstance(message, SystemMessage)
        ]
        validate_tool_pairing([message for _, message in pairs])
        cut, tokens = len(pairs), 0
        while cut and tokens < self._keep:
            cut -= 1
            tokens += estimate_message_tokens(pairs[cut][1])
        while cut:
            try:
                validate_tool_pairing([message for _, message in pairs[:cut]])
                validate_tool_pairing([message for _, message in pairs[cut:]])
                # Multi-message codecs use one entry ID; don't split its rendered group.
                if cut < len(pairs) and pairs[cut - 1][0] == pairs[cut][0]:
                    cut -= 1
                    continue
                break
            except ValueError:
                cut -= 1
        if not cut or signal.aborted:
            return None
        result = await self._summarize(
            SummaryRequest(
                tuple(deepcopy([message for _, message in pairs[:cut]])), request.instructions
            ),
            signal,
        )
        if signal.aborted:
            return None
        if not isinstance(result, SummaryResult) or not result.summary.strip():
            raise ValueError("Summarizer must return a nonempty SummaryResult")
        return CompactionDraft(
            result.summary, pairs[cut][0] if cut < len(pairs) else None, deepcopy(result.usage)
        )
