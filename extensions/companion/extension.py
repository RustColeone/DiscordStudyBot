"""Companion extension: what each student keeps of each person, in mneme-llm (one file per student).

Every engine that places prompt parts (the room, the assistant, Whale-chan's chat) tells her, each turn, who the person
is to her, what they call her, her notes for the message and her mood's news. After each of her messages a watcher lets her learn
from it: how it left her feeling, what is worth remembering, and a day of contact for her closeness with the person.
Pages see where she stands with you as facts (bond, alias, mood). `$alias` names a student your way, `$bond` shows
where each stands with you. It works for any persona from any module, by name, and imports none of them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from contracts import Capability, Contributions, Extension, ModelRequest, PromptPart, Risk, ToolResult, Watcher

try:
    import mneme_llm
except ImportError:  # optional: without it the module adds nothing
    mneme_llm = None

RESET = {"reset", "remove", "clear", "none"}
DAY = 86400
# The engines whose messages are conversation (the Classroom's are reports on work, which teach her nothing of you):
# the room, the assistant and Whale-chan's chat.
ENGINES = ("room", "assistant", "chat")
# What `students: <persona>:` may set for one student, over the settings for all.
OWN = ("learn", "notes", "mood", "met_role", "writer_profile", "reflect_after_notes", "language", "memory_language",
       "decisions", "config", "call_owner")


def usable() -> bool:
    """mneme-llm installed, and new enough to keep aliases (0.2)."""
    return mneme_llm is not None and hasattr(mneme_llm, "Aliases")


def key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def flag(value, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0", "")
    return default if value is None else bool(value)


class Settings:
    def __init__(self, config, folder: Path | None = None):
        self.raw, self.folder = dict(config), folder
        self.personas = tuple(config.get("personas") or ())
        self.engines = tuple(config.get("engines") or ENGINES)
        self.learn = flag(config.get("learn"), True)
        self.notes = flag(config.get("notes"), True)
        self.mood = flag(config.get("mood"), True)
        self.met_role = str(config.get("met_role", "acquaintance") or "")
        self.writer_profile = str(config.get("writer_profile") or "") or None
        self.reflect_after = int(config.get("reflect_after_notes", 5))
        self.language = str(config.get("language") or "")
        self.call_owner = str(config.get("call_owner") or "")  # what her memory calls the owner (else their name)
        self.memory_language = str(config.get("memory_language") or "") or None  # notes also glossed in it
        # Who judges her mood and what is worth keeping: core (her model), or luna (GPT-6 Luna's Decisions API with
        # OPENAI_API_KEY, then the local micro model once it has learned, then her model). Anything else is core.
        self.decisions = "luna" if str(config.get("decisions") or "").strip().lower() == "luna" else "core"
        given = str(config.get("config") or "")  # a mneme-llm config file (TOML), read when her file is new
        path = Path(given).expanduser() if given else None
        self.config_file = str(path if path is None or path.is_absolute() or folder is None else folder / path) \
            if path is not None else None
        self.students = {str(name): dict(value or {}) for name, value in dict(config.get("students") or {}).items()}
        self._own: dict[str, Settings] = {}

    def of(self, persona: str) -> "Settings":
        """One student's settings: hers under `students:`, over the settings for all."""
        if persona not in self.students:
            return self
        if persona not in self._own:
            own = {key: value for key, value in self.students[persona].items() if key in OWN}
            self._own[persona] = Settings({**self.raw, **own, "students": {}}, self.folder)
        return self._own[persona]


class Book:
    """Every student's mneme-llm file, `<data>/<persona>.db` with the space `<persona>`, opened when first needed.

    A file is made only when there is something to keep: an alias, a role from the settings, or what she learns."""

    def __init__(self, folder: Path, *, personas=(), platforms=None, roles=None, settings: Settings | None = None):
        self.folder, self.personas = Path(folder), tuple(personas)
        self.platforms = {str(name): str(platform) for name, platform in (platforms or {}).items()}
        self.roles = {str(subject): str(role) for subject, role in (roles or {}).items()}
        self.settings = settings or Settings({})
        self.opened: dict[str, object] = {}
        self.index = self.folder / "names.json"  # persona → her display name and other names, as engines showed her
        try:
            names = dict(json.loads(self.index.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            names = {}
        self.names = {persona: value if isinstance(value, dict) else {"display_name": str(value)}
                      for persona, value in names.items()}

    def keeps(self, persona: str) -> bool:
        return not self.personas or persona in self.personas

    def shown(self, persona: str) -> str:
        return self.names.get(persona, {}).get("display_name") or persona

    def subject(self, interface: str, sender_id: str) -> str:
        """One person, whichever bot they talk to: `<platform>:<id>` (`whale_chan` counts as `discord`, say)."""
        return f"{self.platforms.get(interface, interface)}:{sender_id}"

    def people(self, subject: str) -> list[tuple[str, str]]:
        """The (interface, sender id) pairs a subject stands for: facts are published for each."""
        platform, _, sender = subject.partition(":")
        return [(name, sender) for name in sorted({platform, *(name for name, value in self.platforms.items()
                                                              if value == platform)})]

    def met(self, persona) -> None:
        value = {"display_name": persona.display_name, "aliases": list(getattr(persona, "aliases", ()))}
        if self.names.get(persona.name) != value:
            self.names[persona.name] = value
            self.folder.mkdir(parents=True, exist_ok=True)
            self.index.write_text(json.dumps(self.names, ensure_ascii=False, indent=1), encoding="utf-8")

    def known(self) -> list[str]:
        found = {path.stem for path in self.folder.glob("*.db")} | set(self.names) | set(self.personas)
        return sorted(persona for persona in found if self.keeps(persona))

    def find(self, name: str, subject: str = "") -> str | None:
        """The student a name stands for: her persona name, display name, other names, or what this person calls
        her."""
        wanted = key(name)
        for persona in self.known():
            value = self.names.get(persona, {})
            if wanted in {key(persona), key(value.get("display_name")), *map(key, value.get("aliases") or ())}:
                return persona
        for persona in self.known():
            memory = self.open(persona, create=False)
            if memory is not None and subject and key(memory.aliases.get(subject) or "") == wanted:
                return persona
        return None

    def open(self, persona: str, *, create: bool = False):
        if persona in self.opened:
            return self.opened[persona]
        path = self.folder / f"{persona}.db"
        if not path.exists() and not create and not self.roles:
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        own = self.settings.of(persona)
        luna = own.decisions == "luna"  # GPT-6 Luna's Decisions API (OPENAI_API_KEY), then the micro model, then hers
        # No meaning tiers that need the network at recall: her part is rendered without waiting, so notes are
        # found by their words, and by their glosses in `memory_language`.
        language = own.memory_language or (mneme_llm.STORED if own.config_file else None)
        memory = mneme_llm.Mneme(str(path), persona, config=own.config_file, companion_name=self.shown(persona),
                                 display_language=own.language or mneme_llm.STORED, luna=luna, micro=luna,
                                 embed=False, meaning_micro=False, memory_language=language)
        for subject, role in self.roles.items():
            memory.relate(subject, role)  # (the same role again changes nothing)
        self.opened[persona] = memory
        return memory

    def stand(self, persona: str, subject: str) -> dict:
        """Where she stands with a person, as facts for the pages: bond, alias, mood (None: nothing to show)."""
        memory = self.open(persona)
        if memory is None:
            return {"bond": None, "alias": None, "mood": None, "plans": None}
        relationship = memory.relations.get(subject)
        state = memory.mood.state(subject)
        now = time.time()
        plans = sorted((note for note in memory.memory.notes(subject, kinds=["plan"])
                        if note.happened_at and now - DAY <= note.happened_at <= now + 30 * DAY),
                       key=lambda note: note.happened_at)[:8]
        return {"bond": {"role": relationship.role, "closeness": round(relationship.closeness)}
                if relationship else None,
                "alias": memory.aliases.get(subject),
                "mood": {"value": round(state.value), "face": state.face,
                         "feelings": [[name, round(value)] for name, value in state.strongest(2, above=20)]}
                if state.updated_at is not None else None,
                # what the person has coming (from yesterday to a month ahead): something to ask them about
                "plans": [{"text": note.text[:160], "at": round(note.happened_at)} for note in plans] or None}

    def publish(self, facts, persona: str, subject: str) -> None:
        if facts is None:
            return
        stand = self.stand(persona, subject)
        for person in self.people(subject):
            for name, value in stand.items():
                facts.set(persona, name, value, person=person)

    def publish_all(self, facts) -> None:
        """At start: what the files already hold, so the pages show it before anyone talks."""
        if facts is None:
            return
        for persona in self.known():
            memory = self.open(persona)
            if memory is None:
                continue
            subjects = {item.subject for item in memory.relations.all()} | set(memory.aliases.all())
            for subject in sorted(subjects):
                self.publish(facts, persona, subject)

    def close(self) -> None:
        for memory in self.opened.values():
            memory.close()
        self.opened.clear()


def no_notes(person: str, language: str) -> str:
    """Her notes when there are none yet: said all the same, so she knows her memory is kept for her (left out, a
    model tends to save what it is told with a tool of its own, which waits for the owner's approval)."""
    return f"关于{person}的笔记：（暂无）" if language.startswith("zh") else f"Notes about {person}: (none yet)"


def impulse(trigger) -> str:
    """Her own motive when the message is a turn she started herself (an interface marks it `extra.speak_up`: Whale-
    chan speaking up), else ""."""
    return str((getattr(trigger, "extra", None) or {}).get("speak_up") or "")


def make_part(book: Book, settings: Settings):
    def text(context) -> str:
        """Who the person is to her, what they call her, her notes for this message and her mood's news (until she has
        replied); nothing for a routine run, or a person she has nothing on."""
        persona, trigger = context.persona, context.trigger
        engine = getattr(context, "engine", "")  # (contracts 1.9; an older core gives none)
        if trigger.origin != "person" or not book.keeps(persona.name) or (engine and engine not in settings.engines):
            return ""
        book.met(persona)
        own = settings.of(persona.name)
        person = own.call_owner if own.call_owner and context.owner else trigger.sender.name or trigger.sender.id
        memory = book.open(persona.name)
        if memory is None:  # (nothing kept of anyone yet)
            return no_notes(person, own.language) if own.learn and own.notes else ""
        subject = book.subject(context.address.interface, trigger.sender.id)
        who = "\n".join(line for line in (memory.relations.line(subject, person),
                                          memory.aliases.line(subject, person)) if line)
        query = impulse(trigger) or trigger.text  # (speaking up, her notes for what moves her)
        notes = (memory.memory.context(subject, query, user_name=person) or no_notes(person, own.language)) \
            if own.notes else ""
        # Her mood's news is told once: marked by the watcher once she has replied, so a step asked twice (or a run
        # replayed) shows it each time; without the watcher, at once.
        news = (memory.mood.news(subject, mark=not own.learn) or "") if own.mood else ""
        return "\n\n".join(part for part in (who, notes, news) if part)
    return text


def writer(models, profile: str | None):
    """mneme-llm's own calls (judging the mood, writing notes, her impression), on her model or `writer_profile`."""
    async def complete(system: str, user: str) -> str:
        result = await models.generate(ModelRequest(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], profile=profile,
            effort="low", max_tokens=1500, timeout_s=60.0))  # reasoning models count their thinking too
        return result.text
    return complete


def make_learner(book: Book, settings: Settings):
    async def learn(exchange, context) -> None:
        """After each of her messages: how it left her feeling and what is worth keeping (one decision call, and
        a note writer only when something is), a day of contact for her closeness, and once a day her impression.
        A message she started herself (speaking up) moves her mood only: notes come from what people tell her."""
        persona, trigger = exchange.persona, exchange.trigger
        if trigger.origin != "person" or exchange.engine not in settings.engines or not book.keeps(persona.name):
            return
        own = settings.of(persona.name)
        if not own.learn:
            return
        book.met(persona)
        memory = book.open(persona.name, create=True)
        subject = book.subject(exchange.address.interface, trigger.sender.id)
        person = own.call_owner if own.call_owner and exchange.owner else trigger.sender.name or trigger.sender.id
        motive = impulse(trigger)
        if own.met_role and not motive and memory.relations.get(subject) is None:
            memory.relate(subject, own.met_role)  # someone new is an acquaintance; your settings say more
        complete = writer(context.models, own.writer_profile)
        memory.mood.news(subject, mark=True)  # she has replied: what moved her before was told
        said = [(f"({persona.display_name}'s own thought)", motive) if motive else (person, trigger.text),
                (persona.display_name, exchange.text)]
        if own.mood or own.notes:
            seen = await memory.aobserve(subject, said, source="speak-up" if motive else "user", user_name=person,
                                         complete=complete, mood=own.mood, capture=own.notes)
            if seen.error:
                context.log("notes_not_written", persona=persona.name, error=seen.error)
        elif not motive:
            memory.relations.seen(subject)
        if own.notes and not motive:
            updated = memory.memory.profile_updated_at(subject)
            if (updated is None or time.time() - updated >= DAY) and \
                    len(memory.memory.notes(subject)) >= own.reflect_after:
                await memory.areflect(subject, user_name=person, complete=complete)
        book.publish(context.facts, persona.name, subject)
    return learn


def make_alias(book: Book, facts=None):
    async def alias(arguments, context) -> ToolResult:
        subject = book.subject(context.address.interface, context.sender.id)
        text = str(arguments.get("text") or "").strip()
        if not text:
            names = {persona: memory.aliases.get(subject) for persona in book.known()
                     if (memory := book.open(persona)) is not None}
            lines = [f"{book.shown(persona)}: {name}" for persona, name in names.items() if name]
            return ToolResult.ok("\n".join(["What you call them:", *lines]) if lines else
                                 "You have not named anyone yet. `$alias <student> <name>` gives one a name of yours.")
        who, _, name = text.partition(" ")
        persona = book.find(who, subject)
        if persona is None:
            shown = ", ".join(book.shown(persona) for persona in book.known()) or "nobody yet"
            return ToolResult.fail(f"No student called {who!r} here (known: {shown}). A student is known once she has "
                                   "spoken in a chat, or from `personas:` in the companion's config.yml.",
                                   "unknown_student")
        shown, name = book.shown(persona), name.strip()
        memory = book.open(persona, create=True)
        if not name:
            present = memory.aliases.get(subject)
            return ToolResult.ok(f"You call {shown} {present}." if present else f"You have no name for {shown} yet.")
        if name.casefold() in RESET:
            memory.alias(subject, None)
            book.publish(facts, persona, subject)
            return ToolResult.ok(f"{shown} is {shown} to you again.")
        try:
            stored = memory.alias(subject, name)
        except ValueError as error:
            return ToolResult.fail(str(error), "invalid_alias")
        book.publish(facts, persona, subject)
        return ToolResult.ok(f"You call {shown} {stored} now; she will know.", data={"persona": persona, "alias": stored})
    return alias


def make_bond(book: Book):
    async def bond(arguments, context) -> ToolResult:
        subject = book.subject(context.address.interface, context.sender.id)
        lines = []
        for persona in book.known():
            stand = book.stand(persona, subject)
            relationship, alias = stand["bond"], stand["alias"]
            line = (f"{relationship['role']}, closeness {relationship['closeness']}/100" if relationship
                    else "no role yet")
            if stand["mood"]:
                line += f", mood {stand['mood']['value']}/100 ({stand['mood']['face']})"
            lines.append(f"{book.shown(persona)}: {line}" + (f"; you call her {alias}" if alias else ""))
        return ToolResult.ok("\n".join(lines) or "No students yet.")
    return bond


def setup(context) -> Contributions:
    if not usable():
        context.log("mneme_llm_missing", hint="pip install -U mneme-llm (0.2 or newer) for the companion")
        return Contributions()
    config = context.config
    settings = Settings(config, Path(context.data_dir))
    book = Book(context.data_dir, personas=settings.personas, platforms=config.get("platforms"),
                roles=config.get("roles"), settings=settings)
    facts = getattr(context, "facts", None)  # (None when loaded without a core)
    book.publish_all(facts)
    return Contributions(
        prompt_parts=[PromptPart("companion", make_part(book, settings), place="turn", order=50,
                                 personas=settings.personas)],
        watchers=[Watcher("companion", make_learner(book, settings), personas=settings.personas, timeout_s=180)]
        if settings.learn or any(flag(own.get("learn"), False) for own in settings.students.values()) else [],
        capabilities=[
            Capability("companion.alias", "Show or set what you call a student.", make_alias(book, facts),
                       {"text": {"type": "string", "description": "<student> <name>, <student> reset, or nothing"}},
                       risk=Risk.WRITE, internal=True, command="alias", command_argument="text"),
            Capability("companion.bond", "Where each student stands with you.", make_bond(book), risk=Risk.READ,
                       internal=True, command="bond"),
        ],
        # Naming a student changes only your own name for her, and only you can type it (models are not offered it).
        permissions={"*": {"rules": {"companion.alias": "auto"}}})


EXTENSION = Extension("companion", setup, contracts="1", version="2",
                      description="What each student keeps of each person (mneme-llm): roles, aliases, notes, mood, "
                                  "plans")
