from pathlib import Path

from app.bot_types import BotResponse, HandlerResult, IncomingMessage


class HelpFeature:
    PAGE_CONTENT_LIMIT = 1850

    def __init__(self, help_path: str = "assets/help.txt"):
        self.help_path = Path(help_path)

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$help")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        help_content = self.help_path.read_text(encoding="utf-8")
        parts = message.content.split()

        if len(parts) >= 2:
            topic = parts[1].lower()
            if topic == "secret":
                if not message.metadata.get("is_creator"):
                    return [BotResponse(text="Topic 'secret' not found.")]
                return [BotResponse(text=self._creator_help())]

            page = 1
            if len(parts) > 3 or (len(parts) == 3 and not parts[2].isdigit()):
                return [BotResponse(text=self._fence("Usage: $help <command> [page]"))]
            if len(parts) == 3:
                page = int(parts[2])

            detailed_text = self._get_topic_section(help_content, topic)
            if detailed_text is not None:
                pages = self._paginate(detailed_text)
                if page < 1 or page > len(pages):
                    return [BotResponse(text=self._fence(
                        f"Page {page} not found for {topic}. Available pages: 1-{len(pages)}"
                    ))]
                return [BotResponse(text=self._format_page(topic, pages, page))]
            topics = ", ".join(self._get_topics(help_content))
            return [BotResponse(text=self._fence(f"Topic '{topic}' not found. Available: {topics}"))]

        if "---DETAILED-HELP---" in help_content:
            help_content = help_content.split("---DETAILED-HELP---", maxsplit=1)[0].strip()

        return [BotResponse(text=self._fence(help_content))]

    @staticmethod
    def _fence(content: str) -> str:
        return f"```md\n{content}\n```"

    @classmethod
    def _format_page(cls, topic: str, pages: list[str], page: int) -> str:
        content = pages[page - 1]
        if len(pages) > 1:
            navigation = [f"Page {page}/{len(pages)}"]
            if page > 1:
                navigation.append(f"Previous: $help {topic} {page - 1}")
            if page < len(pages):
                navigation.append(f"Next: $help {topic} {page + 1}")
            content += "\n\n" + " | ".join(navigation)
        return cls._fence(content)

    @classmethod
    def _paginate(cls, content: str) -> list[str]:
        pages = []
        current = ""
        for paragraph in content.split("\n\n"):
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= cls.PAGE_CONTENT_LIMIT:
                current = candidate
                continue
            if current:
                pages.append(current)
                current = ""
            for line in paragraph.splitlines():
                candidate = f"{current}\n{line}" if current else line
                if len(candidate) <= cls.PAGE_CONTENT_LIMIT:
                    current = candidate
                    continue
                if current:
                    pages.append(current)
                current = line
        if current:
            pages.append(current)
        return pages or [""]

    @staticmethod
    def _get_topics(help_content: str) -> list[str]:
        if "---DETAILED-HELP---" not in help_content:
            return []
        detailed_section = help_content.split("---DETAILED-HELP---", maxsplit=1)[1]
        return [
            line[4:-4].strip().lower()
            for line in detailed_section.splitlines()
            if line.startswith("=== ") and line.endswith(" ===")
        ]

    @staticmethod
    def _creator_help() -> str:
        return (
            "```md\n"
            "# Creator Commands\n"
            "$admin status              Show private creator and queue settings\n"
            "$admin queue               Show command queue usage\n"
            "$admin queue clear         Clear waiting commands; keep the active command running\n"
            "$admin creator-only on     Restrict the bot to the creator for debugging\n"
            "$admin creator-only off    Restore access for other users\n"
            "\nCreator commands jump ahead of waiting user commands. The active command is not interrupted.\n"
            "```"
        )

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
