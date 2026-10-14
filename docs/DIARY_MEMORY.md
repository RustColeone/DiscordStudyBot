# Character diaries

The room has four separate diaries. Each participating character decides whether an explicitly shared personal
fact, preference, goal, or meaningful moment deserves a short note. A diary is a curated recollection, not a record
of everything said. Characters can have different impressions of the same exchange.

For the homemade 烧卖 photo, a possible DeepSeek note is:

> 主人会自己做烧卖，还把照片分享给我们看。下次聊做饭，可以问问馅儿是怎么调的。

That note remembers the moment and suggests a natural future connection. It should not turn one photo into an
unsupported claim that 烧卖 is the user's favorite food or that the user is a professional chef.

## Storage and recall

`character_diary` in `bot_data.db` stores character, user/room/platform scope, stable memory key, kind, note,
importance, and UTC timestamps. Notes are limited to 400 characters, with at most two updates per generated turn.
The model decides significance; updates below importance 3 are ignored. Identical notes do not create duplicates
or refresh their date. Reusing a key replaces a corrected fact. Episode and reflection notes remain dated recollections,
and should not be treated as permanently current facts.

Before a reply, the character receives relevant notes found by English keywords or Chinese character pairs,
plus important notes, within a 3,500-character data budget. Diary data is context rather than instructions. The
latest user correction takes precedence. Each character's own diary is kept separate from other characters'
notes, and from other users or rooms. Chat history still provides short-term continuity independently.

Shared room turns carry a private `diary_updates` array inside their structured JSON response. Legacy
conversation paths can append hidden `<diary_updates>` metadata to their normal reply or a silent `SKIP`
turn. Neither path adds a separate AI request. The application validates the metadata,
assigns ownership from the actual speaker and incoming user identity, saves the selected notes, and removes the
metadata before display or transcript persistence. Malformed or incomplete metadata is hidden and ignored.
Characters that are not queried on a turn do not receive invented diary entries from another character.

## User controls

- `$memory <character> [page]` displays 20 notes per page about the caller in the current room, with note IDs and dates.
- `$memory forget <character> <id>` removes one owned note.
- `$memory clear <character>` removes that character's diary notes about the caller in this room.
- `$chat --clear` resets conversation history while keeping diary notes.

Dates shown by `$memory` use the configured `TIMEZONE`, or explicitly labeled UTC if it is unset. Restarting the
bot initializes the new table without replacing existing conversation data. Diaristic quality and significance
are model decisions; the commands let the user inspect and remove a mistaken recollection.

## Live checks

1. Tell the room a useful fact: `你们，我最近在学粤语。` Check the separate diaries with `$memory DeepSeek` and `$memory Luna`.
2. Ask casual questions or exchange greetings. Confirm the diary does not grow after every turn.
3. Correct a fact: `我已经不学粤语了，改学日语了。` Inspect whether existing language notes were revised rather than duplicated.
4. Clear chat history, then ask a character what she remembers. Her diary should still be available.
5. Ask another user to run `$memory DeepSeek`. They should only see their own notes in that room.
6. Remove a note using its ID and check that it disappears from future diary context.

Automated tests cover storage, ownership, correction, bounded recall, hidden metadata, forgetting, and room
integration without extra generation calls. Live checks assess whether the models choose useful notes and write
them in convincing character voices.
