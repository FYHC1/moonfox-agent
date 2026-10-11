import asyncio
from dataclasses import FrozenInstanceError, dataclass

import pytest

from moonfox_agent.api import AbortToken, PendingQueue, RunOptions, default_convert_to_llm
from moonfox_ai.api import UserMessage


def test_budget_is_required_positive_and_immutable():
    with pytest.raises(TypeError):
        RunOptions()
    for invalid in (0, -1, True, 1.5, "2", None):
        with pytest.raises(ValueError):
            RunOptions(invalid)
    options = RunOptions(2)
    with pytest.raises(FrozenInstanceError):
        options.max_turns_per_run = 3


async def test_abort_is_one_way_and_waitable():
    token = AbortToken()
    assert not token.aborted
    waiter = asyncio.create_task(token.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    token.abort()
    token.abort()
    await waiter
    await token.wait()
    assert token.aborted
    assert not hasattr(token, "reset")
    assert not AbortToken().aborted


def test_queue_modes_order_and_detached_peek():
    queue = PendingQueue()
    message = UserMessage("first", 1)
    queue.enqueue(message)
    queue.enqueue(UserMessage("second", 2))
    queue.enqueue(UserMessage("third", 3))
    message.content = "mutated"
    peeked = queue.peek()
    assert [item.content for item in peeked] == ["first"]
    peeked[0].content = "changed"
    assert [item.content for item in queue.drain()] == ["first"]
    assert queue.has_items()
    queue.mode = "all"
    assert [item.content for item in queue.drain()] == ["second", "third"]
    assert not queue.has_items()
    queue.enqueue(message)
    queue.clear()
    assert queue.peek() == []
    with pytest.raises(ValueError):
        queue.mode = "invalid"
    with pytest.raises(ValueError):
        PendingQueue("invalid")


def test_default_codec_rejects_unknown_roles_instead_of_silently_dropping_them():
    @dataclass
    class Notice:
        text: str
        timestamp: int = 0
        role: str = "notice"

    with pytest.raises(TypeError, match="convert_to_llm"):
        default_convert_to_llm([Notice("important")])
    message = UserMessage("input", 0)
    converted = default_convert_to_llm([message])
    assert converted == [message] and converted[0] is not message
