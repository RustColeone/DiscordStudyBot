import asyncio
from datetime import datetime

from assets import ascii
import pytz
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_reminder_command


timeZoneUTC = pytz.utc
TimeZoneCNBJ = pytz.timezone("Asia/Shanghai")
TimeZoneUSCA = pytz.timezone("America/Los_Angeles")
TimeZoneUKLD = pytz.timezone("Europe/London")


class TimeFeature:
    def __init__(self):
        self.clock_tasks = {}

    def can_handle(self, message: IncomingMessage) -> bool:
        return (
            message.content.startswith("$time")
            or message.content.startswith("$start")
            or message.content.startswith("$stop")
            or message.content.startswith("$remindMeIn")
        )

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if message.content.startswith("$time"):
            time_cnbj, time_usca, time_ukld, timer_m, timer_s = get_time_zone_info()
            return [BotResponse(text=build_clock_string(time_cnbj, time_usca, time_ukld, timer_m, timer_s))]

        if message.content.startswith("$start"):
            if not getattr(app.platform, "supports_message_edit", True):
                return [BotResponse(text="⚠️ Live clock updates are not supported on this platform.")]

            existing_task = self.clock_tasks.get(message.channel_id)
            if existing_task and not existing_task.done():
                existing_task.cancel()

            task = app.platform.create_background_task(self._run_clock(app, message.channel_id))
            self.clock_tasks[message.channel_id] = task
            return [BotResponse(text="Starting Clock")]

        if message.content.startswith("$stop"):
            if not getattr(app.platform, "supports_message_edit", True):
                return [BotResponse(text="⚠️ Live clock updates are not supported on this platform.")]

            task = self.clock_tasks.pop(message.channel_id, None)
            if task is None or task.done():
                return [BotResponse(text="No active clock to stop")]
            task.cancel()
            return [BotResponse(text="Stopping Timer")]

        reminder_command = parse_reminder_command(message.content)
        if reminder_command.errors:
            return [BotResponse(text="❌ " + "\n".join(reminder_command.errors))]

        author_reference = message.metadata.get("author_ref")
        app.platform.create_background_task(
            self._send_reminder(app, author_reference, reminder_command.minutes, reminder_command.message or "")
        )
        return [BotResponse(text="Timer Set")]

    async def _run_clock(self, app, channel_id: str) -> None:
        native_message = await app.platform.send_channel_message(channel_id, "Starting Clock")
        try:
            while True:
                time_cnbj, time_usca, time_ukld, timer_m, timer_s = get_time_zone_info()
                clock_text = build_clock_string(time_cnbj, time_usca, time_ukld, timer_m, timer_s)
                await app.platform.edit_message(native_message, clock_text)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

    async def _send_reminder(self, app, author_reference, minutes: float, reminder_text: str) -> None:
        reminder_suffix = f"to [ {reminder_text}] " if reminder_text else ""
        await asyncio.sleep(minutes * 60)
        await app.platform.send_direct_message(
            author_reference,
            f"boop, you told me to remind you {reminder_suffix}{minutes} minutes ago",
        )


def get_time_zone_info():
    time_cnbj = datetime.now(TimeZoneCNBJ).strftime("CN-BJ> %y/%m/%d %H:%M:%S\n")
    time_usca = datetime.now(TimeZoneUSCA).strftime("US-LA> %y/%m/%d %H:%M:%S\n")
    time_ukld = datetime.now(TimeZoneUKLD).strftime("UK-LD> %y/%m/%d %H:%M:%S")
    timer_m = int(datetime.now(timeZoneUTC).strftime("%M"))
    timer_s = int(datetime.now(timeZoneUTC).strftime("%S"))
    return time_cnbj, time_usca, time_ukld, timer_m, timer_s


def build_clock_string(time_cnbj, time_usca, time_ukld, timer_m, timer_s):
    text = [None] * 5
    codeblock = "```"
    temp_text = codeblock
    break_time_text = "Break Time"
    if timer_m < 45:
        temp_text += "md\n"
        break_time_text = "Not Break Time just Yet"
    temp_text += "#" + "#" * 33 + "\n"
    temp_text += "#" + " " * 32 + "#\n"

    for i in range(5):
        text[i] = "# "
        if timer_m < 10:
            text[i] += ascii.numbers[0][i] + ascii.numbers[timer_m][i]
        else:
            text[i] += ascii.numbers[int(timer_m / 10)][i] + ascii.numbers[timer_m % 10][i]
        text[i] += ascii.coloum[i]
        if timer_s < 10:
            text[i] += ascii.numbers[0][i] + ascii.numbers[timer_s][i]
        else:
            text[i] += ascii.numbers[int(timer_s / 10)][i] + ascii.numbers[timer_s % 10][i]
        temp_text += text[i] + "#\n"
    temp_text += "#" + " " * 32 + "#\n"
    temp_text += "#" + "#" * 33 + "\n"
    temp_text += codeblock + "\n"
    temp_text += break_time_text
    temp_text += codeblock + "ml\n"
    temp_text += "Last updated in \n" + time_cnbj + time_usca + time_ukld + codeblock
    return temp_text
