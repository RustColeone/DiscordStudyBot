from pathlib import Path

from app.bot_types import BotResponse, HandlerResult, IncomingMessage


class HelpFeature:
    def __init__(self, help_path: str = "assets/help.txt"):
        self.help_path = Path(help_path)

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$help")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        help_content = self.help_path.read_text(encoding="utf-8")
        parts = message.content.split(maxsplit=1)

        if len(parts) == 2:
            topic = parts[1].lower().strip()
            detailed_text = self._get_topic_section(help_content, topic)
            if detailed_text is not None:
                return [BotResponse(text=f"```md\n{detailed_text}\n```")]
            return [BotResponse(text=f"Topic '{topic}' not found. Available: chat, music, search, time, db")]

        if "---DETAILED-HELP---" in help_content:
            help_content = help_content.split("---DETAILED-HELP---", maxsplit=1)[0].strip()

        return [BotResponse(text=f"```md\n{help_content}\n```")]

    def _get_topic_section(self, help_content: str, topic: str):
        if "---DETAILED-HELP---" not in help_content:
            return None

        detailed_section = help_content.split("---DETAILED-HELP---", maxsplit=1)[1]
        section_marker = f"=== {topic.upper()} ==="
        if section_marker not in detailed_section:
            return None

        start_index = detailed_section.find(section_marker)
        next_index = detailed_section.find("===", start_index + len(section_marker))
        if next_index == -1:
            return detailed_section[start_index:].strip()
        return detailed_section[start_index:next_index].strip()
