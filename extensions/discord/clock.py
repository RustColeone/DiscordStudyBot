"""Study clock (ported from the legacy `$time` / `$start` / `$stop`): big UTC minutes:seconds with a
break marker from minute 45, plus Beijing, Los Angeles and London times. A live clock is one message
per conversation, edited periodically until stopped."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

DIGITS = [
    [" ####  ", "##  ## ", "##  ## ", "##  ## ", " ####  "],
    ["  ##   ", "####   ", "  ##   ", "  ##   ", "###### "],
    ["#####  ", "    ## ", " ####  ", "##     ", "###### "],
    ["#####  ", "    ## ", " ####  ", "    ## ", "#####  "],
    ["  #    ", " ## #  ", "##  #  ", "###### ", "    #  "],
    ["#####  ", "##     ", "#####  ", "    ## ", "#####  "],
    [" ####  ", "##     ", "#####  ", "#   ## ", " ####  "],
    ["###### ", "    ## ", "   ##  ", "  ##   ", "  ##   "],
    [" ####  ", "##  ## ", "###### ", "##  ## ", " ####  "],
    [" ####  ", "#   ## ", " ##### ", "    ## ", " ####  "],
]
COLON = ["   ", "## ", "   ", "## ", "   "]
ZONES = (("CN-BJ", "Asia/Shanghai"), ("US-LA", "America/Los_Angeles"), ("UK-LD", "Europe/London"))


def time_zone_info(now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    lines = [now.astimezone(ZoneInfo(zone)).strftime(f"{label}> %y/%m/%d %H:%M:%S") for label, zone in ZONES]
    utc = now.astimezone(timezone.utc)
    return lines[0] + "\n", lines[1] + "\n", lines[2], utc.minute, utc.second


def build_clock_string(time_cnbj, time_usca, time_ukld, timer_m, timer_s) -> str:
    fence = "```"
    text = fence
    break_text = "Break Time"
    if timer_m < 45:
        text += "md\n"
        break_text = "Not Break Time just Yet"
    text += "#" * 34 + "\n" + "#" + " " * 32 + "#\n"
    for row in range(5):
        text += ("# " + DIGITS[timer_m // 10][row] + DIGITS[timer_m % 10][row] + COLON[row]
                 + DIGITS[timer_s // 10][row] + DIGITS[timer_s % 10][row] + "#\n")
    text += "#" + " " * 32 + "#\n" + "#" * 34 + "\n" + fence + "\n" + break_text
    text += fence + "ml\n" + "Last updated in \n" + time_cnbj + time_usca + time_ukld + fence
    return text


def clock_text(now: datetime | None = None) -> str:
    return build_clock_string(*time_zone_info(now))


class LiveClocks:
    """One edited clock message per conversation."""

    def __init__(self, channel_for, interval_s: float = 1.0, limit: int = 10):
        self._channel_for = channel_for
        self.interval_s = max(1.0, float(interval_s))
        self.limit = max(1, int(limit))
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, conversation: str) -> bool:
        task = self._tasks.get(conversation)
        return task is not None and not task.done()

    async def start(self, conversation: str) -> str | None:
        """Start (or restart) the clock in a conversation. Returns an error text, or None."""
        await self.stop(conversation)
        if sum(1 for task in self._tasks.values() if not task.done()) >= self.limit:
            return f"At most {self.limit} live clocks can run at once."
        channel = await self._channel_for(conversation)
        if channel is None:
            return "This conversation is not available."
        message = await channel.send(content=clock_text())
        self._tasks[conversation] = asyncio.create_task(self._run(message), name=f"discord-clock-{conversation}")
        return None

    async def _run(self, message) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await message.edit(content=clock_text())
            except (discord.NotFound, discord.Forbidden):
                return
            except discord.HTTPException:
                continue

    async def stop(self, conversation: str) -> bool:
        task = self._tasks.pop(conversation, None)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def stop_all(self) -> None:
        for conversation in list(self._tasks):
            await self.stop(conversation)
