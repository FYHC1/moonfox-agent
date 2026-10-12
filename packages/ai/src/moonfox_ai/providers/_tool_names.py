"""Stable wire aliases for namespaced tools; canonical transcript names never change."""

import hashlib
import re
from typing import Any

_VALID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PREFIX = "mfx_"


def normalize_tool_names(
    payload: dict[str, Any], *, anthropic: bool, responses: bool = False
) -> dict[str, str]:
    """Mutate a freshly built payload and return its wire-to-canonical name map.

    Reserve the alias prefix even for already-valid names to prevent collisions with
    generated aliases. Include historical calls, so replay and current declarations
    have the same spelling independently of the currently enabled tool set.
    """
    references: list[dict[str, Any]] = []
    for tool in payload.get("tools", []):
        references.append(tool if anthropic or responses else tool["function"])
    for message in payload.get("messages", []):
        if message["role"] != "assistant":
            continue
        if anthropic:
            references.extend(block for block in message["content"] if block["type"] == "tool_use")
        else:
            references.extend(call["function"] for call in message.get("tool_calls", []))
    if responses:
        references.extend(
            item for item in payload.get("input", []) if item.get("type") == "function_call"
        )
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for reference in references:
        name = reference["name"]
        alias = name
        if not _VALID.fullmatch(name) or name.startswith(_PREFIX):
            alias = _PREFIX + hashlib.sha256(name.encode("utf-8")).hexdigest()[:60]
        if alias in reverse and reverse[alias] != name:
            raise ValueError("Tool wire-name collision")
        forward[name] = alias
        reverse[alias] = name
    for reference in references:
        reference["name"] = forward[reference["name"]]
    return reverse
