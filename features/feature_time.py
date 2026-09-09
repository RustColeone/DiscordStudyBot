import asyncio
from datetime import datetime, timedelta, timezone

from assets import ascii
import pytz
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_reminder_command
from services import database as db
from services.reminder_service import parse_natural_reminder, resolve_timezone_name


timeZoneUTC = pytz.utc
TimeZoneCNBJ = pytz.timezone("Asia/Shanghai")
TimeZoneUSCA = pytz.timezone("America/Los_Angeles")
TimeZoneUKLD = pytz.timezone("Europe/London")


class TimeFeature:
    def __init__(self):
        self.clock_tasks = {}
        self.reminder_scheduler_task = None

    def can_handle(self, message: IncomingMessage) -> bool:
        return (
            message.content.startswith("$time")
            or message.content.startswith("$start")
            or message.content.startswith("$stop")
            or message.content.startswith("$remind ")
            or message.content == "$remind"
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

        if message.content == "$remind" or message.content.startswith("$remind "):
            return self.create_natural_reminder(app, message, message.content)

        reminder_command = parse_reminder_command(message.content)
        if reminder_command.errors:
            return [BotResponse(text="❌ " + "\n".join(reminder_command.errors))]

        remind_at = message.created_at + timedelta(minutes=reminder_command.minutes)
        reminder_id = self._save_reminder(
            message,
            remind_at,
            reminder_command.message or "Reminder",
        )
        return [BotResponse(text=self._confirmation(reminder_id, remind_at))]

    def create_natural_reminder(self, app, message: IncomingMessage, text: str) -> HandlerResult:
        timezone_name = resolve_timezone_name(app.config.get("TIMEZONE"))
        parsed = parse_natural_reminder(text, message.created_at, timezone_name)
        if parsed is None:
            return [
                BotResponse(
                    text=(
                        "I couldn't determine the reminder time. Try: "
                        "`$remind tomorrow at this time to submit the report`"
                    )
                )
            ]

        reminder_id = self._save_reminder(message, parsed.remind_at, parsed.message)
        return [BotResponse(text=self._confirmation(reminder_id, parsed.remind_at))]

    def _save_reminder(self, message: IncomingMessage, remind_at: datetime, reminder_text: str) -> int:
        platform = "wechat" if message.guild_name == "WeChat" else "discord"
        return db.create_reminder(
            message.channel_id,
            message.author_id,
            message.author_display_name,
            platform,
            remind_at,
            reminder_text,
        )

    def list_reminders(self, message: IncomingMessage, include_all: bool = False) -> str:
        author_id = None if include_all and message.metadata.get("is_creator") else message.author_id
        reminders = db.get_pending_reminders(message.channel_id, author_id)
        if not reminders:
            return "You have no pending reminders in this channel."
        lines = []
        for reminder in reminders:
            remind_at = datetime.fromisoformat(reminder["remind_at"])
            owner = f" for {reminder['author_name']}" if author_id is None else ""
            lines.append(
                f"- **#{reminder['id']}** `{remind_at:%Y-%m-%d %H:%M %Z}`{owner}: {reminder['message']}"
            )
        return "**Pending reminders**\n" + "\n".join(lines)

    def delete_reminder(self, message: IncomingMessage, reminder_id: int) -> str:
        deleted = db.delete_pending_reminder(
            reminder_id,
            message.channel_id,
            message.author_id,
            allow_any_author=bool(message.metadata.get("is_creator")),
        )
        if deleted:
            return f"Deleted reminder #{reminder_id}."
        return f"Reminder #{reminder_id} was not found, is no longer pending, or belongs to another user."

    @staticmethod
    def _confirmation(reminder_id: int, remind_at: datetime) -> str:
        return f"Reminder #{reminder_id} set for `{remind_at:%Y-%m-%d %H:%M:%S %Z}`."

    def start_scheduler(self, app) -> None:
        if self.reminder_scheduler_task is None or self.reminder_scheduler_task.done():
            self.reminder_scheduler_task = app.platform.create_background_task(self._run_reminder_scheduler(app))

    async def _run_reminder_scheduler(self, app) -> None:
        while True:
            for reminder in db.get_due_reminders(datetime.now(timezone.utc)):
                try:
                    recipient = (
                        f"<@{reminder['author_id']}>"
                        if reminder["platform"] == "discord"
                        else reminder["author_name"]
                    )
                    await app.platform.send_channel_message(
                        reminder["channel_id"],
                        f"{recipient} Reminder: {reminder['message']}",
                    )
                    db.mark_reminder_sent(reminder["id"])
                except Exception as error:
                    print(f"Reminder {reminder['id']} delivery failed: {error}")
            await asyncio.sleep(15)

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
