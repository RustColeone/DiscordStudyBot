from services import database as db
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_db_command


class DatabaseFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$db") or message.content in {"$dbStats", "$dbExport", "$dbImport"}

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        cmd = parse_db_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]

        if cmd.action == "export":
            filepath = db.export_to_json()
            stats = db.get_database_stats()
            return [
                BotResponse(
                    text=(
                        f"📦 Database exported to {filepath}\n"
                        f"Stats: {stats['total_messages']} messages, {stats['active_music_sessions']} music sessions"
                    )
                )
            ]

        if cmd.action == "import":
            try:
                db.import_from_json()
                return [BotResponse(text="✅ Database imported successfully")]
            except Exception as error:
                return [BotResponse(text=f"❌ Import failed: {error}")]

        if cmd.action == "stats":
            stats = db.get_database_stats()
            messages_by_ai = ", ".join([f"{name}: {count}" for name, count in stats["messages_by_ai"].items()])
            return [
                BotResponse(
                    text=(
                        "📊 Database Stats:\n"
                        f"Total messages: {stats['total_messages']}\n"
                        f"By AI: {messages_by_ai}\n"
                        f"Music sessions: {stats['active_music_sessions']}"
                    )
                )
            ]

        return [BotResponse(text="❌ No database action specified")]
