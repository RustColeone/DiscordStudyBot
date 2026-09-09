from app.bot_types import BotResponse, HandlerResult, IncomingMessage


class AdminFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content == "$admin" or message.content.startswith("$admin ")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if not message.metadata.get("is_creator"):
            return [BotResponse(text="This command is restricted to the bot creator.")]

        arguments = message.content.split()[1:]
        if not arguments or arguments == ["status"]:
            return [BotResponse(text=self._status(app))]
        if arguments == ["queue"]:
            return [BotResponse(text=self._queue_status(app))]
        if arguments == ["queue", "clear"]:
            removed = app.platform.command_queue.clear()
            for command in removed:
                await command.channel.send("Your pending command was cleared by the bot creator.")
            return [
                BotResponse(
                    text=f"Cleared {len(removed)} waiting command(s). The active command was not interrupted."
                )
            ]
        if len(arguments) == 2 and arguments[0] == "creator-only" and arguments[1] in {"on", "off"}:
            enabled = arguments[1] == "on"
            app.platform.creator_only_mode = enabled
            return [BotResponse(text=f"Creator-only debug mode: {'ON' if enabled else 'OFF'}")]
        return [BotResponse(text="Unknown creator command. Use `$help secret`.")]

    def _status(self, app) -> str:
        return (
            "**Creator Settings**\n"
            f"Creator-only debug mode: `{'on' if app.platform.creator_only_mode else 'off'}`\n"
            f"Private creator prompt: `{'configured' if app.config.get('CREATOR_PROMPT') else 'not configured'}`\n"
            f"{self._queue_status(app)}"
        )

    @staticmethod
    def _queue_status(app) -> str:
        queue = app.platform.command_queue
        return (
            f"Command queue: `{queue.waiting}/{queue.max_waiting}` waiting, "
            f"`{queue.creator_waiting}` creator, active: `{'yes' if queue.active else 'no'}`"
        )