"""Validate untrusted plugin contributions before forwarding detached DTOs."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from copy import deepcopy
from typing import cast

from moonfox_ai.api import ImageContent, TextContent, UserMessage
from moonfox_app.extension_api import ContextBlock, CustomEntryDraft, Delivery, PromptSection

from .errors import NamespaceError, RegistrationError

_SUFFIX = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")


def namespace(owner: str, name: str) -> None:
    prefix = owner + "."
    if (
        not isinstance(name, str)
        or not name.startswith(prefix)
        or _SUFFIX.fullmatch(name[len(prefix) :]) is None
    ):
        raise NamespaceError(f"{owner} must use its own nonempty namespace {prefix!r}: {name!r}")


def _json_shape(value: object, ancestors: set[int]) -> None:
    if value is None or type(value) in (str, bool, int, float):
        return
    if not isinstance(value, (dict, list)):
        raise RegistrationError("Entry data must contain only JSON values")
    if id(value) in ancestors:
        raise RegistrationError("Entry data must not contain cycles")
    ancestors.add(id(value))
    try:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise RegistrationError("Entry data object keys must be strings")
                _json_shape(child, ancestors)
        else:
            for child in value:
                _json_shape(child, ancestors)
    finally:
        ancestors.remove(id(value))


def drafts(owner: str, value: Sequence[CustomEntryDraft]) -> tuple[CustomEntryDraft, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RegistrationError("Entries must be a sequence of CustomEntryDraft")
    detached = deepcopy(tuple(value))
    for draft in detached:
        if not isinstance(draft, CustomEntryDraft) or type(draft.context_required) is not bool:
            raise RegistrationError("Entries must be CustomEntryDraft with a boolean context flag")
        namespace(owner, draft.custom_type)
        try:
            _json_shape(draft.data, set())
            json.dumps(draft.data, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise RegistrationError(f"{owner}: entry data is not JSON-safe") from exc
    return detached


def prompt(owner: str, value: object) -> tuple[PromptSection, ...]:
    if not isinstance(value, tuple):
        raise RegistrationError("Prompt provider must return a tuple of PromptSection")
    names: set[str] = set()
    for section in value:
        if not isinstance(section, PromptSection) or not isinstance(section.text, str):
            raise RegistrationError("Prompt provider returned an invalid section")
        namespace(owner, section.name)
        if section.name in names:
            raise RegistrationError(f"Duplicate prompt section: {section.name}")
        names.add(section.name)
    return deepcopy(cast(tuple[PromptSection, ...], value))


def context(value: object) -> tuple[ContextBlock, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(block, ContextBlock)
        or not isinstance(block.source, str)
        or not block.source.strip()
        or not isinstance(block.text, str)
        for block in value
    ):
        raise RegistrationError("Context provider must return a tuple of sourced ContextBlock")
    return deepcopy(cast(tuple[ContextBlock, ...], value))


def user_message(message: UserMessage, delivery: Delivery) -> UserMessage:
    if delivery not in ("steer", "follow_up"):
        raise RegistrationError("Invalid message delivery")
    if (
        type(message) is not UserMessage
        or message.role != "user"
        or type(message.timestamp) is not int
    ):
        raise RegistrationError("Extensions can enqueue only UserMessage")
    if not isinstance(message.content, str):
        if not isinstance(message.content, list):
            raise RegistrationError("Invalid user message content")
        for block in message.content:
            if type(block) is TextContent:
                if not isinstance(block.text, str) or block.type != "text":
                    raise RegistrationError("Invalid user text content")
            elif type(block) is ImageContent:
                if (
                    not isinstance(block.data, str)
                    or not isinstance(block.mime_type, str)
                    or block.type != "image"
                ):
                    raise RegistrationError("Invalid user image content")
            else:
                raise RegistrationError(
                    "User content cannot contain system, thinking, or tool data"
                )
    return deepcopy(message)
