"""Responses SSE lifecycle: typed items, append-only deltas and explicit terminal events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..types import TextContent, ThinkingContent, Usage
from ._remote import (
    MessageState,
    json_object,
    request_json,
    wire_int,
    wire_object,
    wire_string,
)
from ._sse import InvalidResponse, SSEFrame

REASONING_SIGNATURE = "openai-responses:"


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list) or len(value) > 1024:
        raise InvalidResponse()
    return value


def _text_part(raw: Any) -> tuple[str, str]:
    part = wire_object(raw)
    kind = wire_string(part.get("type"))
    if kind not in ("output_text", "refusal"):
        raise InvalidResponse()
    return kind, wire_string(part.get("text" if kind == "output_text" else "refusal"))


def reasoning_item(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("type") != "reasoning":
        raise InvalidResponse()
    summary = []
    for value in _list(raw.get("summary", [])):
        part = wire_object(value)
        if part.get("type") != "summary_text":
            raise InvalidResponse()
        summary.append({"type": "summary_text", "text": wire_string(part.get("text"))})
    result: dict[str, Any] = {
        "type": "reasoning",
        "id": wire_string(raw.get("id"), nonempty=True),
        "summary": summary,
    }
    if raw.get("encrypted_content") is not None:
        result["encrypted_content"] = wire_string(raw["encrypted_content"], nonempty=True)
    return result


@dataclass(slots=True)
class _Part:
    kind: str
    content_index: int


@dataclass(slots=True)
class _Item:
    id: str
    kind: str
    content_index: int = -1
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    arguments_done: bool = False
    parts: dict[int, _Part] = field(default_factory=dict)
    summaries: list[str] = field(default_factory=list)
    summary_ended: set[int] = field(default_factory=set)
    final: dict[str, Any] | None = None


class _ResponseState:
    def __init__(self, state: MessageState, supports_tools: bool) -> None:
        self.state = state
        self.supports_tools = supports_tools
        self.items: dict[int, _Item] = {}
        self.ids: set[str] = set()

    def identity(self, raw: dict[str, Any]) -> None:
        for key, attr in (("id", "response_id"), ("model", "response_model")):
            value = wire_string(raw.get(key), nonempty=True)
            previous = getattr(self.state.message, attr)
            if previous is not None and value != previous:
                raise InvalidResponse()
            setattr(self.state.message, attr, value)

    def add(self, index: int, raw: dict[str, Any]) -> _Item:
        if index != len(self.items) or index >= 1024:
            raise InvalidResponse()
        identity = wire_string(raw.get("id"), nonempty=True)
        kind = wire_string(raw.get("type"))
        if identity in self.ids or raw.get("status") not in (
            None,
            "in_progress",
            "completed",
            "incomplete",
        ):
            raise InvalidResponse()
        live = _Item(identity, kind)
        self.ids.add(identity)
        self.items[index] = live
        if kind == "message":
            if raw.get("role") != "assistant":
                raise InvalidResponse()
            for part_index, part in enumerate(_list(raw.get("content", []))):
                self.add_part(live, part_index, part)
        elif kind == "function_call":
            if not self.supports_tools:
                raise InvalidResponse()
            live.call_id = wire_string(raw.get("call_id"), nonempty=True)
            live.name = wire_string(raw.get("name"), nonempty=True)
            live.content_index = self.state.tool_start(live.call_id, live.name)
            live.arguments = wire_string(raw.get("arguments", ""))
            if live.arguments:
                self.state.tool_delta(live.content_index, live.arguments)
        elif kind == "reasoning":
            live.content_index = self.state.thinking_start()
            for summary in reasoning_item(raw)["summary"]:
                self.add_summary(live, len(live.summaries), summary)
                live.summary_ended.add(len(live.summaries) - 1)
        else:
            # Hosted tools/audio are not silently presented as ordinary function calls.
            raise InvalidResponse()
        return live

    def item(self, data: dict[str, Any], *, done: bool = False) -> _Item:
        index = wire_int(data.get("output_index"))
        if index not in self.items:
            raise InvalidResponse()
        live = self.items[index]
        identity = wire_object(data.get("item")).get("id") if done else data.get("item_id")
        if identity != live.id or live.final is not None:
            raise InvalidResponse()
        return live

    def add_part(self, live: _Item, index: int, raw: Any) -> _Part:
        if live.kind != "message" or index != len(live.parts) or index >= 1024:
            raise InvalidResponse()
        kind, text = _text_part(raw)
        part = _Part(kind, self.state.text_start(text))
        live.parts[index] = part
        return part

    def part(self, live: _Item, data: dict[str, Any]) -> _Part:
        index = wire_int(data.get("content_index"))
        if live.kind != "message" or index not in live.parts:
            raise InvalidResponse()
        return live.parts[index]

    def finish_part(self, part: _Part, text: str) -> None:
        block = self.state.message.content[part.content_index]
        assert isinstance(block, TextContent)
        if not text.startswith(block.text):
            raise InvalidResponse()
        if text != block.text:
            self.state.text_delta(part.content_index, text[len(block.text) :])
        if part.content_index not in self.state.ended:
            self.state.end(part.content_index)

    def arguments(self, live: _Item, text: str) -> None:
        if live.kind != "function_call" or not text.startswith(live.arguments):
            raise InvalidResponse()
        if live.arguments_done and text != live.arguments:
            raise InvalidResponse()
        if text != live.arguments:
            self.state.tool_delta(live.content_index, text[len(live.arguments) :])
            live.arguments = text
        live.arguments_done = True

    def add_summary(self, live: _Item, index: int, raw: Any) -> None:
        part = wire_object(raw)
        if (
            live.kind != "reasoning"
            or part.get("type") != "summary_text"
            or index != len(live.summaries)
            or index >= 1024
            or (index > 0 and index - 1 not in live.summary_ended)
        ):
            raise InvalidResponse()
        text = wire_string(part.get("text"))
        live.summaries.append(text)
        if index or text:
            self.state.thinking_delta(live.content_index, ("\n" if index else "") + text)

    def summary(self, live: _Item, data: dict[str, Any], event: str) -> None:
        index = wire_int(data.get("summary_index"))
        if live.kind != "reasoning" or index >= len(live.summaries):
            raise InvalidResponse()
        if event.endswith(".delta"):
            if index in live.summary_ended or index != len(live.summaries) - 1:
                raise InvalidResponse()
            text = wire_string(data.get("delta"))
            live.summaries[index] += text
            self.state.thinking_delta(live.content_index, text)
            return
        if event == "response.reasoning_summary_part.done":
            part = wire_object(data.get("part"))
            if part.get("type") != "summary_text":
                raise InvalidResponse()
            text = wire_string(part.get("text"))
        else:
            text = wire_string(data.get("text"))
        previous = live.summaries[index]
        if not text.startswith(previous) or (index in live.summary_ended and text != previous):
            raise InvalidResponse()
        if text != previous:
            self.state.thinking_delta(live.content_index, text[len(previous) :])
            live.summaries[index] = text
        live.summary_ended.add(index)

    def finish_item(self, live: _Item, raw: dict[str, Any]) -> None:
        if raw.get("id") != live.id or raw.get("type") != live.kind:
            raise InvalidResponse()
        status = raw.get("status")
        if status is None:
            status = "completed"
        if status not in ("completed", "incomplete"):
            raise InvalidResponse()
        canonical: dict[str, Any] = {"id": live.id, "type": live.kind, "status": status}
        if live.kind == "message":
            if raw.get("role") != "assistant":
                raise InvalidResponse()
            parts = [_text_part(part) for part in _list(raw.get("content"))]
            canonical["content"] = parts
        elif live.kind == "function_call":
            if raw.get("call_id") != live.call_id or raw.get("name") != live.name:
                raise InvalidResponse()
            canonical["arguments"] = wire_string(raw.get("arguments"))
        else:
            canonical = reasoning_item(raw)
        if live.final is not None:
            if live.final != canonical:
                raise InvalidResponse()
            return
        if live.kind == "message":
            if len(parts) < len(live.parts):
                raise InvalidResponse()
            for index, (kind, text) in enumerate(parts):
                part = live.parts.get(index)
                if part is None:
                    part = self.add_part(
                        live,
                        index,
                        {"type": kind, "text" if kind == "output_text" else "refusal": ""},
                    )
                if part.kind != kind:
                    raise InvalidResponse()
                self.finish_part(part, text)
        elif live.kind == "function_call":
            self.arguments(live, canonical["arguments"])
            if status == "completed":
                self.state.tool_end(live.content_index, json_object(live.arguments))
        else:
            summaries = [part["text"] for part in canonical["summary"]]
            if len(summaries) < len(live.summaries):
                raise InvalidResponse()
            for index, text in enumerate(summaries):
                if index >= len(live.summaries):
                    self.add_summary(live, index, {"type": "summary_text", "text": ""})
                self.summary(live, {"summary_index": index, "text": text}, "done")
            block = self.state.message.content[live.content_index]
            assert isinstance(block, ThinkingContent)
            if canonical.get("encrypted_content"):
                block.thinking_signature = REASONING_SIGNATURE + request_json(canonical)
            self.state.end(live.content_index)
        live.final = canonical

    def finish(self, response: dict[str, Any], event: str) -> None:
        self.identity(response)
        status = "completed" if event == "response.completed" else "incomplete"
        if response.get("status") != status or response.get("error") is not None:
            raise InvalidResponse()
        reason = "completed"
        if status == "incomplete":
            reason = wire_string(wire_object(response.get("incomplete_details")).get("reason"))
            if reason != "max_output_tokens":
                raise InvalidResponse()
        output = _list(response.get("output"))
        if len(output) < len(self.items):
            raise InvalidResponse()
        for index, value in enumerate(output):
            raw = wire_object(value)
            live = self.items.get(index)
            if live is None:
                live = self.add(index, raw)
            self.finish_item(live, raw)
            if status == "completed" and raw.get("status") not in (None, "completed"):
                raise InvalidResponse()
        if response.get("usage") is not None:
            _usage(wire_object(response["usage"]), self.state.message.usage)
        self.state.discard_incomplete_calls()
        message = self.state.message
        message.raw_stop_reason = reason
        message.stop_reason = (
            "length"
            if status == "incomplete"
            else "tool_use"
            if self.state.complete_calls
            else "stop"
        )
        message.end_turn = message.stop_reason == "stop"


async def consume_responses(
    frames: AsyncIterator[SSEFrame], state: MessageState, *, supports_tools: bool
) -> None:
    response = _ResponseState(state, supports_tools)
    started = False
    async for frame in frames:
        data = json_object(frame.data)
        event = wire_string(data.get("type"), nonempty=True)
        if frame.event not in ("message", event):
            raise InvalidResponse()
        if event == "response.created":
            if started:
                raise InvalidResponse()
            response.identity(wire_object(data.get("response")))
            started = True
            continue
        if not started:
            raise InvalidResponse()
        if event in ("response.in_progress", "response.queued"):
            response.identity(wire_object(data.get("response")))
            continue
        if event in ("response.completed", "response.incomplete"):
            response.finish(wire_object(data.get("response")), event)
            return  # Responses has no Chat Completions [DONE] requirement.
        if event == "response.output_item.added":
            response.add(wire_int(data.get("output_index")), wire_object(data.get("item")))
            continue
        if event in ("response.failed", "response.cancelled", "error"):
            raise InvalidResponse()
        live = response.item(data, done=event == "response.output_item.done")
        if event == "response.output_item.done":
            response.finish_item(live, wire_object(data.get("item")))
        elif event == "response.content_part.added":
            response.add_part(live, wire_int(data.get("content_index")), data.get("part"))
        elif event == "response.content_part.done":
            part = response.part(live, data)
            kind, text = _text_part(data.get("part"))
            if kind != part.kind:
                raise InvalidResponse()
            response.finish_part(part, text)
        elif event in (
            "response.output_text.delta",
            "response.refusal.delta",
            "response.output_text.done",
            "response.refusal.done",
        ):
            part = response.part(live, data)
            if event.split(".")[1] != part.kind:
                raise InvalidResponse()
            if event.endswith(".delta"):
                state.text_delta(part.content_index, wire_string(data.get("delta")))
            else:
                response.finish_part(
                    part, wire_string(data.get("text" if part.kind == "output_text" else "refusal"))
                )
        elif event == "response.output_text.annotation.added":
            if response.part(live, data).kind != "output_text":
                raise InvalidResponse()
            # Annotations are not executable tools or public text deltas.
            wire_object(data.get("annotation"))
        elif event == "response.function_call_arguments.delta":
            if live.kind != "function_call" or live.arguments_done:
                raise InvalidResponse()
            delta = wire_string(data.get("delta"))
            live.arguments += delta
            state.tool_delta(live.content_index, delta)
        elif event == "response.function_call_arguments.done":
            response.arguments(live, wire_string(data.get("arguments")))
        elif event == "response.reasoning_summary_part.added":
            response.add_summary(live, wire_int(data.get("summary_index")), data.get("part"))
        elif event in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_summary_part.done",
        ):
            response.summary(live, data, event)
        else:
            raise InvalidResponse()
    raise InvalidResponse()  # EOF is not a successful terminal event.


def _usage(data: dict[str, Any], usage: Usage) -> None:
    total_input = wire_int(data.get("input_tokens"))
    usage.output = wire_int(data.get("output_tokens"))
    details = wire_object(
        {} if data.get("input_tokens_details") is None else data["input_tokens_details"]
    )
    usage.cache_read = wire_int(details.get("cached_tokens", 0))
    if usage.cache_read > total_input:
        raise InvalidResponse()
    usage.input = total_input - usage.cache_read
    details = wire_object(
        {} if data.get("output_tokens_details") is None else data["output_tokens_details"]
    )
    if details.get("reasoning_tokens") is not None:
        usage.reasoning = wire_int(details["reasoning_tokens"])
        if usage.reasoning > usage.output:
            raise InvalidResponse()
    usage.total_tokens = wire_int(data.get("total_tokens", total_input + usage.output))
