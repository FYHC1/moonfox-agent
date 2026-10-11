"""Conservative provider-error recognition; recovery policy belongs to callers."""

import re

from .types import AssistantMessage

OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bprompt is too long\b",
        r"\brequest_too_large\b",
        r"\bcontext_length_exceeded\b",
        r"\bexceeds? (?:the |this |model |available |maximum )*context (?:window|length|size)\b",
        r"\bmaximum (?:context|prompt) length\b",
        r"\binput token count\b.*\bexceeds\b.*\bmaximum\b",
        r"\breduce the length of the messages or completion\b",
        r"\binput\b.*\blonger than\b.*\bcontext length\b",
    )
)


def is_context_overflow(message: AssistantMessage) -> bool:
    return message.stop_reason == "error" and any(
        pattern.search(message.error_message or "") for pattern in OVERFLOW_PATTERNS
    )


def is_recoverable_length(message: AssistantMessage) -> bool:
    return message.stop_reason == "length"
