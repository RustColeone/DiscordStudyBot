import asyncio

from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from services.system_status import SystemStatus, collect_system_status


class SystemFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        parts = message.content.strip().split(maxsplit=1)
        return bool(parts) and parts[0] in {"$system", "$status"}

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if len(message.content.strip().split()) > 1:
            return [BotResponse(text="Usage: `$system` or `$status`")]

        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(None, collect_system_status)
        return [BotResponse(text=format_system_status(status))]


def format_system_status(status: SystemStatus) -> str:
    temperature = "Unavailable"
    if status.temperature_c is not None:
        temperature = f"{status.temperature_c:.1f} C"

    return (
        "**System Status**\n"
        f"Time: `{status.timestamp:%Y-%m-%d %H:%M:%S %Z}`\n"
        f"Temperature: `{temperature}`\n"
        f"CPU: `{status.cpu_percent:.1f}%` ({status.cpu_count} cores)\n"
        f"Memory: `{_format_gib(status.memory_used)} / {_format_gib(status.memory_total)}` "
        f"({status.memory_percent:.1f}%)\n"
        f"Storage: `{_format_gib(status.storage_used)} / {_format_gib(status.storage_total)}` "
        f"({status.storage_percent:.1f}%)"
    )


def _format_gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GiB"