"""A rolling Discord status block for messages nobody explicitly asked for (copy kept inside the extension).

Lines for one destination are appended to a single code-block message by editing it. A block
accepts lines for up to an hour (or until it would exceed Discord's message limit); after that it
is released from memory and the next line starts a new block.
"""

import asyncio
import time
from collections import defaultdict

WINDOW_SECONDS = 3600
MESSAGE_LIMIT = 1900


def code_safe(text):
    # A literal ``` would close the block early.
    return str(text).replace("```", "ˋˋˋ")


def render(lines):
    return "```\n" + "\n".join(lines) + "\n```"


class StatusLog:
    def __init__(self, send, edit, clock=time.monotonic, window=WINDOW_SECONDS, limit=MESSAGE_LIMIT):
        self._send, self._edit, self._clock = send, edit, clock
        self.window, self.limit = window, limit
        self._blocks = {}
        self._locks = defaultdict(asyncio.Lock)

    def _prune(self, now):
        for destination in [key for key, block in self._blocks.items() if now - block["opened"] >= self.window]:
            del self._blocks[destination]

    async def append(self, destination, text):
        stamp = time.strftime("%H:%M:%S")
        body = code_safe(text).strip().replace("\n", "\n  ")
        line = f"{stamp} {body}"[: self.limit - 20]
        async with self._locks[destination]:
            now = self._clock()
            self._prune(now)
            block = self._blocks.get(destination)
            if block is not None:
                lines = block["lines"] + [line]
                content = render(lines)
                if len(content) <= self.limit:
                    try:
                        await self._edit(block["message"], content)
                        block["lines"] = lines
                        return block["message"]
                    except Exception:
                        # The message was deleted or cannot be edited: start a new block.
                        pass
            message = await self._send(destination, render([line]))
            if message is None:
                self._blocks.pop(destination, None)
            else:
                self._blocks[destination] = {"message": message, "lines": [line], "opened": now}
            return message
