"""Standalone dispatch semantics: callbacks are copied, ordered and validated."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from moonfox_app.extension_api import ExtensionDiagnostic, PermissionDecision
from moonfox_app.extensions import Dispatcher, Handler


def validate(value: list[int]) -> None:
    if not isinstance(value, list) or any(type(item) is not int or item < 0 for item in value):
        raise ValueError("nonnegative integer list required")


async def test_chain_retains_previous_valid_snapshot_after_exception_or_invalid_result() -> None:
    diagnostics: list[ExtensionDiagnostic] = []
    original = [1]
    retained: list[list[int]] = []
    seen: list[list[int]] = []

    def good(value: list[int]) -> list[int]:
        value.append(2)
        retained.append(value)
        return value

    async def broken(value: list[int]) -> list[int]:
        value.append(999)
        raise ValueError("half finished")

    def invalid(value: list[int]) -> list[int]:
        value.append(-1)
        return value

    def last(value: list[int]) -> list[int]:
        seen.append(list(value))
        return value + [3]

    dispatcher = Dispatcher(diagnose=diagnostics.append)
    result = await dispatcher.chain(
        [
            Handler("good", good),
            Handler("broken", broken),
            Handler("invalid", invalid),
            Handler("last", last),
        ],
        original,
        validate,
    )
    retained[0].append(88)
    assert result == [1, 2, 3] and original == [1] and seen == [[1, 2]]
    assert [item.extension_id for item in diagnostics] == ["broken", "invalid"]


async def test_inplace_handlers_modify_validated_copies_not_live_or_previous_values() -> None:
    diagnostics: list[ExtensionDiagnostic] = []
    original = [1]
    retained: list[list[int]] = []

    def good(value: list[int]) -> None:
        value.append(2)
        retained.append(value)

    async def broken(value: list[int]) -> None:
        value.clear()
        raise RuntimeError("discard partial modifications")

    def invalid(value: list[int]) -> None:
        value.append(-1)

    def wrong_return(value: list[int]) -> None:
        value.append(100)
        return cast(None, value)

    def last(value: list[int]) -> None:
        assert value == [1, 2]
        value.append(3)

    dispatcher = Dispatcher(diagnose=diagnostics.append)
    result = await dispatcher.mutate(
        [
            Handler("good", good),
            Handler("broken", broken),
            Handler("invalid", invalid),
            Handler("wrong", wrong_return),
            Handler("last", last),
        ],
        original,
        validate,
    )
    retained[0].append(99)
    assert result == [1, 2, 3] and original == [1]
    assert [item.extension_id for item in diagnostics] == ["broken", "invalid", "wrong"]


@pytest.mark.parametrize("policy", ["chain", "mutate"])
async def test_invalid_initial_snapshot_is_never_dispatched(policy: str) -> None:
    dispatcher = Dispatcher()
    with pytest.raises(ValueError):
        if policy == "chain":
            await dispatcher.chain([], [-1], validate)
        else:
            await dispatcher.mutate([], [-1], validate)


async def test_notify_snapshots_registration_sequence_and_payload_for_each_listener() -> None:
    diagnostics: list[ExtensionDiagnostic] = []
    handlers: list[Handler[dict[str, list[int]], None]] = []
    calls: list[str] = []
    original = {"values": [1]}
    detached: list[dict[str, list[int]]] = []

    def first(value: dict[str, list[int]]) -> None:
        value["values"].append(2)
        handlers.clear()
        calls.append("first")

    async def second(value: dict[str, list[int]]) -> None:
        assert value == original
        detached.append(value)
        calls.append("second")
        raise RuntimeError("isolate")

    def third(value: dict[str, list[int]]) -> None:
        assert value == original
        calls.append("third")

    handlers.extend([Handler("first", first), Handler("second", second), Handler("third", third)])
    await Dispatcher(diagnose=diagnostics.append).notify(handlers, original)
    detached[0]["values"].append(99)
    assert calls == ["first", "second", "third"] and original == {"values": [1]}
    assert len(diagnostics) == 1


@pytest.mark.parametrize(
    "kind",
    ["deny", "raise", "invalid", "invalid_allowed", "timeout", "cancelled", "swallow_timeout"],
)
async def test_gate_fail_closed_short_circuit(kind: str) -> None:
    calls: list[str] = []
    diagnostics: list[ExtensionDiagnostic] = []

    async def gate(value: list[int]) -> PermissionDecision:
        calls.append("gate")
        if kind == "deny":
            return PermissionDecision(False, "explicit deny")
        if kind == "raise":
            raise RuntimeError("offline")
        if kind == "invalid":
            return cast(PermissionDecision, True)
        if kind == "invalid_allowed":
            return PermissionDecision(cast(Any, 1))
        if kind == "cancelled":
            raise asyncio.CancelledError()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if kind == "swallow_timeout":
                return PermissionDecision(True)
            raise
        return PermissionDecision(True)

    def last(value: list[int]) -> PermissionDecision:
        calls.append("last")
        return PermissionDecision(True)

    decision = await Dispatcher(timeout=0.01, diagnose=diagnostics.append).gate(
        [Handler("gate", gate), Handler("last", last)], [1]
    )
    assert decision.allowed is False and calls == ["gate"]
    assert bool(diagnostics) is (kind != "deny")


async def test_gate_allow_returns_valid_decision_and_does_not_share_requests() -> None:
    def first(value: list[int]) -> PermissionDecision:
        value.append(2)
        return PermissionDecision(True)

    def second(value: list[int]) -> PermissionDecision:
        assert value == [1]
        return PermissionDecision(True)

    assert (await Dispatcher().gate([Handler("a", first), Handler("b", second)], [1])).allowed
    assert (await Dispatcher().gate([], [1])).allowed


@pytest.mark.parametrize("policy", ["notify", "chain", "mutate", "gate"])
async def test_external_cancellation_is_preserved_not_isolated(policy: str) -> None:
    entered = asyncio.Event()
    finalized = asyncio.Event()
    diagnostics: list[ExtensionDiagnostic] = []

    async def blocked(value: list[int]) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    dispatcher = Dispatcher(diagnose=diagnostics.append)

    async def dispatch() -> None:
        if policy == "notify":
            await dispatcher.notify([Handler("x", blocked)], [1])
        elif policy == "chain":
            await dispatcher.chain([Handler[list[int], list[int]]("x", blocked)], [1], validate)
        elif policy == "mutate":
            await dispatcher.mutate([Handler("x", blocked)], [1], validate)
        else:
            await dispatcher.gate([Handler("x", blocked)], [1])

    task = asyncio.create_task(dispatch())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized.is_set() and diagnostics == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_timeout_must_be_finite_positive(timeout: float) -> None:
    with pytest.raises(ValueError):
        Dispatcher(timeout=timeout)


async def test_callback_cannot_convert_requested_cancellation_to_allow() -> None:
    entered = asyncio.Event()

    async def suppress(value: list[int]) -> PermissionDecision:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return PermissionDecision(True)
        return PermissionDecision(True)

    async def dispatch() -> PermissionDecision:
        return await Dispatcher().gate([Handler("suppress", suppress)], [1])

    task = asyncio.create_task(dispatch())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
