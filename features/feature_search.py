from providers import google_query as googleQuery
from providers import wolfram_query as wolframQuery
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_google_command, parse_wolfram_command


class SearchFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$wolfram") or message.content.startswith("$google")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if message.content.startswith("$wolfram"):
            return await self._handle_wolfram(message)
        return await self._handle_google(message)

    async def _handle_wolfram(self, message: IncomingMessage) -> HandlerResult:
        cmd = parse_wolfram_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]
        if not cmd.query:
            return [BotResponse(text="❌ No query provided")]

        answer = wolframQuery.queryWolfram(cmd.query)
        return [BotResponse(text=f"Wolfram Replied>\n```md\n{answer}\n```")]

    async def _handle_google(self, message: IncomingMessage) -> HandlerResult:
        cmd = parse_google_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]
        if not cmd.query:
            return [BotResponse(text="❌ No query provided")]

        titles, links, descriptions = googleQuery.queryGoogle(cmd.query)
        lines = [f"Google Replied> **{cmd.query}**", ""]
        for index, title in enumerate(titles):
            lines.append(f"{index + 1}. [{title}]({links[index]})")
            lines.append(descriptions[index])
            lines.append("")

        return [BotResponse(text="\n".join(lines).strip())]
