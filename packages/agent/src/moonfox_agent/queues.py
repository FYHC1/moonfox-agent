"""Independent FIFO primitive. Run scheduling is introduced in the next stage."""

from copy import deepcopy
from typing import Literal

from .types import AgentMessage

type QueueMode = Literal["all", "one_at_a_time"]


class PendingQueue:
    def __init__(self, mode: QueueMode = "one_at_a_time") -> None:
        self._items: list[AgentMessage] = []
        self.mode = mode

    @property
    def mode(self) -> QueueMode:
        return self._mode

    @mode.setter
    def mode(self, value: QueueMode) -> None:
        if value not in ("all", "one_at_a_time"):
            raise ValueError("Queue mode must be all or one_at_a_time")
        self._mode = value

    def enqueue(self, message: AgentMessage) -> None:
        self._items.append(deepcopy(message))

    def has_items(self) -> bool:
        return bool(self._items)

    def peek(self) -> list[AgentMessage]:
        return deepcopy(self._items if self.mode == "all" else self._items[:1])

    def drain(self) -> list[AgentMessage]:
        drained = self.peek()
        del self._items[: len(drained)]
        return drained

    def clear(self) -> None:
        self._items.clear()
