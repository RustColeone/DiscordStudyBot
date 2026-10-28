"""What a speaker reads of the conversation: the thread since `/new`, its latest messages word for word (the window),
the older ones as the conversation's rolling summary, and the images shared shortly before.

`$commands` and their answers are not part of it (`$model`'s lists, `$help`): they are for the person who typed them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import time

from contracts import FileRef, ModelError, ModelRequest, Session

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
NEW_THREAD = {"/new", "/reset", "/clear"}
# How an interface marks a message it sent on a persona's own impulse (with `extra.speak_up`, Whale-chan's bot
# speaking up): in history it is her impulse, not something the person said.
IMPULSE = "⟨speak-up⟩ "
COMPACT_RETRY_S = 15 * 60  # after a failed summary call, wait this long before trying again
COMPACT = """You keep the running summary of a group conversation, so its participants can follow it once the messages themselves are out of view. Rewrite the summary to cover the new messages too: who said what that matters, decisions, plans, promises, open questions, facts people shared about themselves, and the mood. Drop small talk and anything already settled. Plain text, in the conversation's main language, at most 300 words."""


def is_image(file: FileRef) -> bool:
    return (file.mime or "").startswith("image/") or file.path.lower().endswith(IMAGE_SUFFIXES)


def add_images(state: dict, files) -> None:
    """Remember image attachments of the run's messages and tool results (latest 6) so every step can see them."""
    for file in files or ():
        item = dataclasses.asdict(file) if dataclasses.is_dataclass(file) else dict(file)
        if (item.get("mime") or "").startswith("image/") or item["path"].lower().endswith(IMAGE_SUFFIXES):
            state["images"] = [*state.get("images", []), item][-6:]


def images(state: dict) -> tuple[FileRef, ...]:
    return tuple(FileRef(**item) for item in state.get("images", []))


def recent_images(items, now: float, window_s: float, limit: int = 4) -> list[FileRef]:
    """Images people sent shortly before this request (WeChat sends a picture and its question separately), newest
    last."""
    found: list[FileRef] = []
    for item in sorted(items, key=lambda entry: entry.at, reverse=True):
        if now - item.at > window_s or len(found) >= limit:
            break
        if item.direction == "in":
            found.extend(file for file in reversed(item.files) if is_image(file))
    return list(reversed(found[:limit]))


def combine_images(*groups) -> tuple[FileRef, ...]:
    seen, result = set(), []
    for file in (file for group in groups for file in group):
        if file.path not in seen:
            seen.add(file.path)
            result.append(file)
    return tuple(result[-6:])


def is_command(text: str) -> bool:
    """A `$command`, as the core tells them apart (`$` and a letter)."""
    text = text.strip()
    return len(text) > 1 and text[0] == "$" and text[1].isalpha()


def line(item, named: bool, shown: set[str]) -> str:
    """One readable entry from someone else: time, speaker (in a group), text, what it replied to, attachments."""
    if item.direction == "in" and item.text.startswith(IMPULSE):
        return f"(You messaged them on your own then: {item.text[len(IMPULSE):].strip()})"
    when = dt.datetime.fromtimestamp(item.at).astimezone().strftime("%m-%d %H:%M")
    speaker = (item.sender_name or ("assistant" if item.direction == "out" else item.sender_id))
    text = f"[{when}] {speaker + ': ' if named or item.direction == 'out' else ''}{item.text}"
    if item.quoted_text:
        text += f"\n(replying to: {item.quoted_text[:300]})"
    for file in item.files:
        text += f"\n[{'image' if is_image(file) else 'file'} {file.name or 'attachment'}"
        text += ", shown to you with this request]" if file.path in shown else "]"
    return text


def number(value, default: float, low: float, high: float, kind=int):
    """A numeric setting clamped to [low, high]; a value that is not a number (or not finite) is the default."""
    if value is None or isinstance(value, bool):
        return kind(default)
    try:
        result = kind(value) if not isinstance(value, str) else kind(float(value.strip()))
    except (TypeError, ValueError, OverflowError):
        return kind(default)
    if isinstance(result, float) and not math.isfinite(result):
        return kind(default)
    return max(kind(low), min(result, kind(high)))


def flag(value, default: bool) -> bool:
    """An on/off setting as people write it in YAML or JSON: true/false, yes/no, on/off, 1/0. Anything else (or
    nothing) is the default, so `"false"` is never mistaken for on."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower() if isinstance(value, str) else ""
    return True if text in ("true", "yes", "on", "1") else False if text in ("false", "no", "off", "0") else default


class Reading:
    """The conversation as one run's speakers read it (the engine's settings under the assignment's options)."""

    def __init__(self, session: Session, settings):
        self.session, self.settings = session, settings
        options = session.options
        self.limit = number(options.get("history_limit") or None, settings.history_limit, 1, 150)
        self.chars = number(options.get("history_chars"), settings.history_chars, 0, 400000)
        self.compact = flag(options.get("compact"), settings.compact)
        self.compact_every = number(options.get("compact_every"), settings.compact_every, 1, 1000)
        self.compact_profile = str(options.get("compact_profile") or settings.compact_profile)
        self.image_window = number(options.get("history_image_minutes"), 30, 0, 24 * 60, float) * 60

    # ---- the thread -------------------------------------------------------------------------------------------
    def thread_since(self) -> float:
        """When this conversation's thread began: its last `/new` (0: never). A thread begun with the chat engine of a
        student's own bot (kept in her memory then) counts too."""
        session = self.session
        since = float(session.store(conversation=True).get("thread_since") or 0)
        for persona in session.personas:
            since = max(since, float(session.memory(persona.name).get(f"thread_since:{session.address}") or 0))
        return since

    def conversation(self, reach: int = 200) -> list:
        """The thread before this request, without `$commands` and their answers. "Before" is by position: everything
        recorded ahead of the trigger, even in the same second (some platforms send whole seconds)."""
        session = self.session
        items = session.history.recent(reach)
        trigger = session.trigger.id
        cut = next((index for index in range(len(items) - 1, -1, -1)
                    if items[index].direction == "in" and items[index].message_id == trigger), None)
        if cut is not None:
            items = items[:cut]
        else:  # not recorded (or scrolled out of reach): by time
            items = [item for item in items if item.at < session.trigger.received_at]
        since = self.thread_since()
        commands = {item.message_id for item in items if item.direction == "in" and is_command(item.text)}
        items = [item for item in items if item.at > since and item.text.strip().lower() not in NEW_THREAD
                 and not (item.direction == "in" and is_command(item.text))
                 and not (item.direction == "out" and item.reply_to and item.reply_to in commands)]
        if session.options.get("history_scope") == "sender" and not session.trigger.is_direct:
            # Only this member's own messages and the replies that answered them.
            sender = session.trigger.sender.id
            theirs = {item.message_id for item in items if item.direction == "in" and item.sender_id == sender}
            items = [item for item in items if (item.direction == "in" and item.sender_id == sender)
                     or (item.direction == "out" and item.reply_to in theirs)]
        return items

    def split(self) -> tuple[list, list]:
        """The thread as (older, window): the window is the last `history_limit` messages (within `history_chars`,
        newest first, when that is set), read word for word; the summary covers the older ones, so each message is in
        one or the other. A second is never split between them: the summary's `upto` is a time."""
        items = self.conversation()
        start = max(0, len(items) - self.limit)
        if self.chars:
            used = 0
            for index in range(len(items) - 1, start - 1, -1):
                used += len(items[index].text)
                if used > self.chars and index < len(items) - 1:
                    start = index + 1
                    break
        while 0 < start < len(items) and items[start - 1].at >= items[start].at:
            start -= 1
        return items[:start], items[start:]

    def window(self) -> list:
        return self.split()[1]

    def summary(self):
        """The rolling summary, if it covers this thread (one written before the last `/new` is not read)."""
        summary = self.session.history.summary()
        if summary is None or not self.compact or summary.upto <= self.thread_since():
            return None
        return summary

    def shown_images(self, items) -> list[FileRef]:
        return recent_images(items, self.session.trigger.received_at, self.image_window)

    async def fold(self) -> None:
        """Fold messages that scrolled out of the window into the conversation's rolling summary, once enough have
        (`compact_every`), with one call to `compact_profile` (the cheap default). A failure leaves the old summary;
        the conversation goes on either way."""
        if not self.compact:
            return
        session = self.session
        store = session.store(conversation=True)
        if float(store.get("compact_retry_at", 0) or 0) > time.time():
            return  # it failed recently: not on every message (each try costs a model call)
        summary = self.summary()
        covered = summary.upto if summary and math.isfinite(summary.upto) else self.thread_since()
        older, _ = self.split()
        fresh = [item for item in older if item.at > covered]
        if len(fresh) < self.compact_every:
            return
        lines = "\n".join(line(item, True, set()) for item in fresh)
        try:
            result = await session.models.generate(ModelRequest(
                [{"role": "system", "content": COMPACT},
                 {"role": "user", "content": f"Summary so far:\n{summary.text if summary else '(none yet)'}\n\n"
                                             f"New messages:\n{lines}"}],
                profile=self.compact_profile, max_tokens=900, effort="low"))
        except ModelError:
            store.set("compact_retry_at", time.time() + COMPACT_RETRY_S)
            return
        if result.text.strip():
            try:
                session.history.summarize(result.text.strip()[:8000], upto=fresh[-1].at)
            except ValueError:  # refused (a message recorded with a broken time, say): like a failed call
                store.set("compact_retry_at", time.time() + COMPACT_RETRY_S)
