from datetime import datetime, timezone

import pytz

from app.bot_types import BotResponse
from services import database as db
from services.character_diary import CHARACTERS, diary_scope


class MemoryFeature:
    def can_handle(self, message) -> bool:
        return message.content == "$memory" or message.content.startswith("$memory ")

    async def handle(self, app, message):
        parts = message.content.split()
        usage = (
            "Each character keeps her own diary about you in this room.\n"
            "`$memory <character> [page]` — read her notes\n"
            "`$memory forget <character> <note-id>` — remove one note\n"
            "`$memory clear <character>` — remove her notes about you\n"
            "Characters: DeepSeek, Luna, Gemini, Claude."
        )
        if len(parts) == 1:
            return [BotResponse(text=usage)]
        action = parts[1].lower()
        name = parts[2] if action in {"forget", "clear"} and len(parts) > 2 else parts[1]
        character = next((value for value in CHARACTERS if value.lower() == name.lower()), None)
        if character is None:
            return [BotResponse(text=usage)]
        scope = diary_scope(message)
        if action == "forget" and len(parts) == 4 and parts[3].isdecimal() and len(parts[3]) <= 18:
            removed = db.delete_diary_note(scope, character, int(parts[3]))
            return [BotResponse(text="Diary note removed." if removed else "That note was not found in your diary.")]
        if action == "clear" and len(parts) == 3:
            count = db.clear_diary_notes(scope, character)
            return [BotResponse(text=f"Removed {count} notes from {character}'s diary about you in this room.")]
        if len(parts) not in {2, 3} or action in {"forget", "clear"}:
            return [BotResponse(text=usage)]
        if len(parts) == 3 and (not parts[2].isdecimal() or len(parts[2]) > 9):
            return [BotResponse(text=usage)]
        page = int(parts[2]) if len(parts) == 3 else 1
        count = db.count_diary_notes(scope, character)
        if not count:
            return [BotResponse(text=f"{character} hasn't kept any diary notes about you in this room yet.")]
        pages = (count + 19) // 20
        if page < 1 or page > pages:
            return [BotResponse(text=f"Page {page} not found. Available diary pages: 1–{pages}.")]
        notes = db.list_diary_notes(scope, character, limit=20, offset=(page - 1) * 20)
        lines = [f"**{character}'s diary about you**", f"Page {page}/{pages}"]
        try:
            local_zone = pytz.timezone(app.config.get("TIMEZONE") or "UTC")
        except pytz.UnknownTimeZoneError:
            local_zone = pytz.UTC
        for note in sorted(notes, key=lambda item: (item["created_at"], item["id"]), reverse=True):
            recorded = datetime.fromisoformat(note["updated_at"]).replace(tzinfo=timezone.utc).astimezone(local_zone)
            text = note["note"].replace("@", "@\u200b")
            lines.append(f"`#{note['id']}` · {recorded:%Y-%m-%d} {recorded.tzname()} — {text}")
        if page < pages:
            lines.append(f"Next: `$memory {character} {page + 1}`")
        return [BotResponse(text="\n\n".join(lines))]
