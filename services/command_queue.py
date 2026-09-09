from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Tuple


@dataclass(frozen=True)
class QueuedCommand:
    message: Any
    channel: Any
    is_creator: bool


class CommandQueue:
    def __init__(self, max_waiting: int = 5):
        self.max_waiting = max_waiting
        self._creator_queue: Deque[QueuedCommand] = deque()
        self._user_queue: Deque[QueuedCommand] = deque()
        self._available = None
        self.active = False

    @property
    def waiting(self) -> int:
        return len(self._creator_queue) + len(self._user_queue)

    @property
    def creator_waiting(self) -> int:
        return len(self._creator_queue)

    def submit(self, command: QueuedCommand) -> Tuple[bool, Optional[QueuedCommand]]:
        if not command.is_creator and self.waiting >= self.max_waiting:
            return False, None

        target = self._creator_queue if command.is_creator else self._user_queue
        target.append(command)
        if self._available is not None:
            self._available.set()
        return True, None

    def clear(self) -> List[QueuedCommand]:
        removed = list(self._creator_queue) + list(self._user_queue)
        self._creator_queue.clear()
        self._user_queue.clear()
        return removed

    async def get(self) -> QueuedCommand:
        import asyncio

        if self._available is None:
            self._available = asyncio.Event()
        while not self.waiting:
            self._available.clear()
            await self._available.wait()
        if self._creator_queue:
            return self._creator_queue.popleft()
        return self._user_queue.popleft()