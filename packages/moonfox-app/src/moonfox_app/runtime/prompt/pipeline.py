"""Every request starts with canonical projection, never yesterday's enhanced context."""

import html
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from moonfox_agent.api import AgentTool, validate_tool_pairing
from moonfox_ai.api import (
    Message,
    SystemMessage,
    UserMessage,
    collapse_system_messages,
    estimate_message_tokens,
    get_current_system_message,
    get_current_tools,
    get_tool_state_changes,
    now_ms,
)
from moonfox_app.extension_api import ContextBlock, PromptSection

_SECTION_PREFIX = "extension:"


class ContextBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptPipelineOptions:
    max_reference_tokens: int = 2048
    max_context_tokens: int | None = None

    def __post_init__(self) -> None:
        if type(self.max_reference_tokens) is not int or self.max_reference_tokens < 0:
            raise ValueError("max_reference_tokens must be a nonnegative integer")
        if self.max_context_tokens is not None and (
            type(self.max_context_tokens) is not int or self.max_context_tokens < 1
        ):
            raise ValueError("max_context_tokens must be a positive integer or None")


def prompt_delta(
    messages: Sequence[Message], sections: Sequence[PromptSection], tools: Sequence[AgentTool]
) -> SystemMessage | None:
    names = [section.name for section in sections]
    if len(names) != len(set(names)) or any("." not in name for name in names):
        raise ValueError("Prompt sections must be uniquely namespaced")
    baseline = get_current_system_message(messages)
    old = (baseline.sections or {}) if baseline else {}
    desired = {_SECTION_PREFIX + section.name: section.text for section in sections}
    changes: dict[str, str | None] = {
        name: None for name in old if name.startswith(_SECTION_PREFIX) and name not in desired
    }
    changes.update({name: text for name, text in desired.items() if old.get(name) != text})
    tool_changes = get_tool_state_changes(
        get_current_tools(messages), [t.declaration() for t in tools]
    )
    if not changes and not tool_changes.tools_added and not tool_changes.tools_removed:
        return None
    return SystemMessage(
        "",
        now_ms(),
        sections=changes or None,
        tools_added=tool_changes.tools_added or None,
        tools_removed=tool_changes.tools_removed or None,
    )


def request_messages(
    canonical: Sequence[Message], blocks: Sequence[ContextBlock], options: PromptPipelineOptions
) -> list[Message]:
    messages = deepcopy(list(canonical))
    validate_tool_pairing(messages)
    references: list[Message] = []
    remaining = options.max_reference_tokens
    for block in blocks:
        if (
            not isinstance(block, ContextBlock)
            or not block.source
            or not isinstance(block.text, str)
        ):
            raise ValueError("Invalid context reference")
        # Escaping is framing, not a claim of semantic prompt-injection immunity.
        reference = UserMessage(
            f'<reference source="{html.escape(block.source, quote=True)}">\n'
            f"{html.escape(block.text)}\n</reference>",
            now_ms(),
        )
        tokens = estimate_message_tokens(reference)
        if tokens <= remaining:
            references.append(reference)
            remaining -= tokens
    # Before conversation, never between an assistant tool call and its results.
    insertion = next(
        (i for i, m in enumerate(messages) if not isinstance(m, SystemMessage)), len(messages)
    )
    messages[insertion:insertion] = references
    if options.max_context_tokens is not None:
        tokens = sum(
            estimate_message_tokens(message) for message in collapse_system_messages(messages)
        )
        if tokens > options.max_context_tokens:
            raise ContextBudgetExceeded(
                f"Estimated context {tokens} exceeds {options.max_context_tokens}"
            )
    validate_tool_pairing(messages)
    return messages
