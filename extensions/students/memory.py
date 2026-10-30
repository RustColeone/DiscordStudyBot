"""A student's memory: what she keeps of each person, in mneme-llm (one file per student). It was the companion module
until 2026-10-27; a student is an agent with this memory, so it is part of what the students module adds.

Every engine that places prompt parts (MomoChat) tells her, each turn, who the person is to her, what they call her,
her notes for the message, what was said before that bears on it (word for word: her first look, with mneme-llm 0.3),
her mood's news and how her classmates seem today. After each of her messages a watcher lets her learn from it: the exchange kept word for word,
how it left her feeling, what is worth remembering, and a day of contact for her closeness with the person. Two
tools let her search everything said before with the person, and read an exchange with its neighbours, when the
first look does not have it. What was said in a group chat, every student there heard: the exchange and the notes
one keeps of it go into each classmate's file too; a direct chat stays between the two. A person with several accounts (`people:`, or linked with `$link`) is one person to every
student. Pages see where she stands with you as facts (bond, alias, mood, plans, what she calls you, your accounts).
`$alias` names a student your way, `$callme` says what the students call you, `$link` joins another account of yours,
`$bond` shows where each stands with you. Agents (personas that are not students) keep nothing.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from pathlib import Path

from contracts import Capability, ModelRequest, PromptPart, Risk, ToolResult, Watcher

import mneme_llm
from mneme_llm.decide import Predicate
from mneme_llm.tools import SPECS, Toolbox

RESET = {"reset", "remove", "clear", "none"}
DAY = 86400
NAME_MAX = 40  # what the students call you
CODE_S = 600  # a `$link` code lasts ten minutes
CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # (no 0/O, 1/I/L)
# The engines whose messages are conversation (Chimera's are reports on work, which teach her nothing of you), and
# MomoChat's names before (the room, the assistant, the chat engine of a student's own Discord bot), for settings
# that still name them.
ENGINES = ("momochat",)
FORMER = {"room": "momochat", "assistant": "momochat", "chat": "momochat"}
# What `students: <persona>:` (or her student.yml `memory:`, under it) may set for one student, over the settings for
# all.
OWN = ("learn", "notes", "mood", "classmates", "met_role", "owner_role", "writer_profile", "reflect_after_notes", "language",
       "memory_language", "decisions", "config", "call_owner", "episodes", "said", "said_skip", "embed", "search")
# What was said, kept word for word (mneme-llm 0.3): a file of an older mneme-llm keeps notes only.
EPISODES = hasattr(mneme_llm.Mneme, "alook")
TOOLBOXES = 64  # the newest runs' search tools kept (each remembers what its searches showed)
# Interfaces whose people are another's (`platforms:` adds to it): Whale-chan's own bot is on Discord.
PLATFORMS = {"whale_chan": "discord"}


def conversation(engine: str, settings: "Settings") -> bool:
    """Whether messages in this engine are conversation she learns from (an older core names none: yes)."""
    return not engine or FORMER.get(engine, engine) in settings.engines


def key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def flag(value, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0", "")
    return default if value is None else bool(value)


def whole(value, default: int, low: int, high: int) -> int:
    """A whole-number setting within [low, high]; one that is not a number is the default."""
    try:
        number = int(value) if value is not None and not isinstance(value, bool) else default
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


class Settings:
    def __init__(self, config, folder: Path | None = None):
        self.raw, self.folder = dict(config), folder
        self.personas = tuple(config.get("personas") or ())
        self.engines = tuple(FORMER.get(str(name), str(name)) for name in config.get("engines") or ENGINES)
        self.learn = flag(config.get("learn"), True)
        self.notes = flag(config.get("notes"), True)
        self.mood = flag(config.get("mood"), True)
        self.classmates = flag(config.get("classmates"), True)  # how the others seem today, in her turn
        self.met_role = str(config.get("met_role", "acquaintance") or "")
        self.owner_role = str(config.get("owner_role", "creator") or "")  # the bot's owner, when they first talk
        self.writer_profile = str(config.get("writer_profile") or "") or None
        self.reflect_after = int(config.get("reflect_after_notes", 5))
        self.language = str(config.get("language") or "")
        self.call_owner = str(config.get("call_owner") or "")  # what her memory calls the owner (else their name)
        self.memory_language = str(config.get("memory_language") or "") or None  # notes also glossed in it
        # What was said, word for word: each exchange kept (`episodes`), a first look of `said` characters of it for
        # each message (0: none), leaving out the newest `said_skip` exchanges of the chat (the engine still shows
        # them), found by meaning as well as words with `embed` (OpenAI embeddings, OPENAI_API_KEY), and `search`:
        # her tools to look through the rest.
        self.episodes = flag(config.get("episodes"), True)
        self.said = whole(config.get("said"), 6000, 0, 50_000)
        self.said_skip = whole(config.get("said_skip"), 15, 0, 200)
        self.embed = flag(config.get("embed"), True)
        self.search = flag(config.get("search"), True)
        # Who judges her mood and what is worth keeping: luna (GPT-6 Luna's Decisions API with OPENAI_API_KEY, then
        # the local micro model, which learns from Luna, then her model), or core (her model alone, nothing learned).
        self.decisions = "core" if str(config.get("decisions") or "").strip().lower() == "core" else "luna"
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

    def __init__(self, folder: Path, *, personas=(), platforms=None, roles=None, people=None,
                 settings: Settings | None = None):
        self.folder, self.personas = Path(folder), tuple(personas)
        self.platforms = {str(name): str(platform) for name, platform in (platforms or {}).items()}
        self.roles = {self.named(subject): str(role) for subject, role in (roles or {}).items()}
        # One person behind several accounts (`people: {Leo: [discord:1, webui:creator]}`): their first account is
        # where every student keeps them, and the name is what her memory calls them.
        self.accounts: dict[str, list[str]] = {}  # a person's own subject → all of theirs, their own first
        self.person_of: dict[str, str] = {}
        self.person_names: dict[str, str] = {}
        for name, given in dict(people or {}).items():
            subjects = [self.named(item) for item in (given if isinstance(given, (list, tuple)) else [given])
                        if str(item or "").strip()]
            subjects = [item for index, item in enumerate(subjects) if item not in subjects[:index]]
            if not subjects or any(item in self.person_of for item in subjects):
                continue  # (an account named for two people: neither link is made)
            self.accounts[subjects[0]] = subjects
            self.person_names[subjects[0]] = str(name)
            self.person_of.update({item: subjects[0] for item in subjects})
        # What people asked the students to call them (`$callme`: "*" for every student, or one student's name) and
        # the accounts they joined themselves (`$link`), kept in people.json; `people:` wins where the two disagree.
        self.own_file = self.folder / "people.json"
        try:
            own = dict(json.loads(self.own_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            own = {}
        self.calls = {str(person): {str(who): str(name) for who, name in dict(names).items()}
                      for person, names in dict(own.get("calls") or {}).items() if isinstance(names, dict)}
        self.links: dict[str, str] = {}
        for account, person in dict(own.get("links") or {}).items():
            self.join(str(account), str(person))
        self.codes: dict[str, tuple[str, float]] = {}  # a `$link` code → (the person who asked for it, until when)
        self.settings = settings or Settings({})
        self.opened: dict[str, object] = {}
        self.toolboxes: dict[tuple[str, str], Toolbox] = {}  # (run, student) → her search tools in that run
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

    def named(self, subject) -> str:
        """A subject as the settings write it (`whale_chan:42`), as the files keep it (`discord:42`)."""
        interface, _, sender = str(subject).strip().partition(":")
        return self.subject(interface, sender) if sender else interface

    def name_of(self, subject: str) -> str:
        """The person's name in `people:` ("" for someone not named there)."""
        return self.person_names.get(self.person_of.get(subject, ""), "")

    def person(self, subject: str) -> str:
        """The person's own subject: their first account in `people:`, the account they linked from, or itself."""
        return self.person_of.get(subject, subject)

    def call(self, subject: str, who: str = "*") -> str:
        """What the person asked to be called by a student (`who`), or by every student ("*"); "" when they did not."""
        return self.calls.get(self.person(subject), {}).get(who, "")

    def set_call(self, subject: str, who: str, name: str | None) -> None:
        names = self.calls.setdefault(self.person(subject), {})
        if name:
            names[who] = name
        else:
            names.pop(who, None)
        self.save()

    def join(self, account: str, person: str) -> bool:
        """`account` is another account of `person` (to every student, from now on); False when `people:` already
        says whose it is, or it is somebody's own first account."""
        person = self.person(person)
        if account == person or account in self.person_of or account in self.accounts:
            return False
        self.links[account] = person
        self.accounts.setdefault(person, [person]).append(account)
        self.person_of.setdefault(person, person)
        self.person_of[account] = person
        for memory in self.opened.values() if hasattr(self, "opened") else ():
            self.link(memory)
        return True

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        calls = {person: names for person, names in self.calls.items() if names}
        self.own_file.write_text(json.dumps({"calls": calls, "links": self.links}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

    def code_for(self, subject: str) -> str:
        """A one-time code (ten minutes) that joins whichever account sends it back to this person."""
        now = time.time()
        self.codes = {code: value for code, value in self.codes.items() if value[1] > now}
        code = "".join(secrets.choice(CODE_LETTERS) for _ in range(6))
        self.codes[code] = (self.person(subject), now + CODE_S)
        return code

    def redeem(self, code: str) -> str | None:
        """The person a code was given to (it is used up), or None for a wrong or late code."""
        person, until = self.codes.pop(code.strip().upper(), ("", 0.0))
        return person if until > time.time() else None

    def people(self, subject: str) -> list[tuple[str, str]]:
        """The (interface, sender id) pairs the person a subject stands for is reached by, on every account of theirs
        (`people:`) and every interface sharing its platform: facts are published for each."""
        found = set()
        for account in self.accounts.get(self.person_of.get(subject, subject), [subject]):
            platform, _, sender = account.partition(":")
            found |= {(name, sender) for name in {platform, *(name for name, value in self.platforms.items()
                                                             if value == platform)}}
        return sorted(found)

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
        # Notes are found by their words (and their glosses in `memory_language`); what was said, by meaning too
        # (`embed`: embeddings with OPENAI_API_KEY, the query's waited for a few seconds at most).
        language = own.memory_language or (mneme_llm.STORED if own.config_file else None)
        memory = mneme_llm.Mneme(str(path), persona, config=own.config_file, companion_name=self.shown(persona),
                                 display_language=own.language or mneme_llm.STORED, luna=luna, micro=luna,
                                 embed=own.embed and EPISODES, meaning_micro=False, memory_language=language)
        self.link(memory)
        for subject, role in self.roles.items():
            memory.relate(subject, role)  # (the same role again changes nothing)
        self.opened[persona] = memory
        return memory

    def link(self, memory) -> None:
        """Her file's links as `people:` has them: each account of a person is that person (what it kept joins
        them), and an account taken out of `people:` is its own again (what was joined stays)."""
        for subject, person in memory.people.all().items():
            if self.person_of.get(subject) != person:
                memory.unlink(subject)
        for person, subjects in self.accounts.items():
            for subject in subjects[1:]:
                if memory.person(subject) != person:
                    memory.link(subject, person)

    def stand(self, persona: str, subject: str) -> dict:
        """Where she stands with a person, as facts for the pages: bond, alias, mood (None: nothing to show)."""
        memory = self.open(persona)
        calling, accounts = self.calling(persona, subject), self.accounts.get(self.person(subject), [subject])
        own = {"called": calling if any(calling.values()) else None,  # (None: nothing asked or set)
               "accounts": list(accounts) if len(accounts) > 1 else None}
        if memory is None:
            return {"bond": None, "alias": None, "mood": None, "plans": None, **own}
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
                "plans": [{"text": note.text[:160], "at": round(note.happened_at)} for note in plans] or None, **own}

    def calling(self, persona: str, subject: str) -> dict:
        """What she calls the owner, and why (for the pages): `name` (None: their name where they write), `asked`
        (what they asked of her), `everyone` (what they asked of every student), `setting` (call_owner)."""
        asked, everyone = self.call(subject, persona), self.call(subject)
        alone = str(self.settings.students.get(persona, {}).get("call_owner") or "")
        setting = alone or self.settings.call_owner
        name = asked or alone or everyone or self.settings.call_owner or self.name_of(subject)
        return {"name": name or None, "asked": asked or None, "everyone": everyone or None, "setting": setting or None}

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

    def toolbox(self, persona: str, run: str):
        """Her search tools in one run (they remember what their searches showed, so a search again brings new
        exchanges), or None when she keeps nothing of what was said."""
        memory = self.open(persona)
        if memory is None or not EPISODES:
            return None
        key = (run, persona)
        if key not in self.toolboxes:
            while len(self.toolboxes) >= TOOLBOXES:
                del self.toolboxes[next(iter(self.toolboxes))]
            self.toolboxes[key] = Toolbox(memory)
        return self.toolboxes[key]

    def close(self) -> None:
        for memory in self.opened.values():
            memory.close()
        self.opened.clear()
        self.toolboxes.clear()


def no_notes(person: str, language: str) -> str:
    """Her notes when there are none yet: said all the same, so she knows her memory is kept for her (left out, a
    model tends to save what it is told with a tool of its own, which waits for the owner's approval)."""
    return f"关于{person}的笔记：（暂无）" if language.startswith("zh") else f"Notes about {person}: (none yet)"


# Is the person asking about something from before? Then we search for her: left to herself, a model that answers at once
# rather says it does not remember. Luna tells in 0.2 s, the local model once it has learned. With no answer (Luna down,
# the local model unsure) she is searched for all the same: a search costs little, and without one she makes things up.
RECALL = "recall"
ASKS_BACK = Predicate(RECALL, "The person is asking about something from before: a fact about themselves or the people "
                              "and pets around them, something they said or did, or something that happened earlier, "
                              "that the assistant would need to remember to answer. Not a new topic, a request for "
                              "ideas, or a question about the assistant.")
RECALL_FOUND = 5  # exchanges a search adds to her first look
RECALL_NOTES = 6  # and her closest notes
CLOSEST = {"en": "The notes of yours closest to this:", "zh": "你的笔记里和这件事最接近的："}


async def asks_back(memory, query: str) -> bool:
    if not memory.decider.tiers:
        return False
    try:
        found = (await memory.decider.adecide(query, [ASKS_BACK])).get(RECALL)
    except Exception:
        return True
    return found is None or found.probability >= 0.5


async def first_look(memory, subject: str, query: str, person: str, conversation: str, own) -> str:
    """What was said that bears on the message. When they ask about something from before, also what a search finds,
    past what has faded out of the look. Their same question asked before is left out: all it holds is what she said
    then, and an "I don't remember" found again is said again."""
    asked = await asyncio.gather(memory.episodes.alook(subject, query, budget=own.said, conversation=conversation,
                                                       skip=own.said_skip, cut=memory.said_cut),
                                 asks_back(memory, query) if own.search else _nothing(False))
    found = list(asked[0])
    if asked[1]:
        shown = {item.id for item in found} | on_screen(memory, subject, conversation, own.said_skip)
        found += [item for item in await memory.episodes.asearch(subject, query, limit=RECALL_FOUND + 3, seen=shown)][:RECALL_FOUND]
    same = "".join(query.split())
    found = sorted((item for item in found if "".join(item.lines[0][1].split()) != same), key=lambda item: item.at)
    said = memory.episodes.render(found, user_name=person, search=own.search)
    closest = await closest_notes(memory, subject, query) if asked[1] and own.notes else []
    head = CLOSEST["zh" if own.language.startswith("zh") else "en"]
    return "\n\n".join(part for part in (said, "\n".join([head, *closest]) if closest else "") if part)


def on_screen(memory, subject: str, conversation: str, skip: int) -> set[int]:
    """The newest exchanges of this chat, which the engine shows anyway (the look leaves them out too)."""
    return {row["id"] for row in memory.store.rows(
        "select id from episodes where space = ? and subject = ? and conversation = ? order by at desc limit ?",
        (memory.episodes.space, memory.episodes.who(subject), conversation, int(skip)))} if skip > 0 else set()


async def closest_notes(memory, subject: str, query: str) -> list[str]:
    """Her notes nearest in meaning, by rank. The usual recall keeps a note only above a floor, and a short question
    and a short note score low ("what car do I drive?" against the note of it: 0.33, floor 0.41, and first of 291)."""
    meaning = memory.memory.meaning
    if meaning is None:
        return []
    notes = {note.id: note for note in memory.memory.notes(subject)}
    found = await meaning.find(list(notes.values()), query)
    ranked = sorted(found.similarities.items(), key=lambda pair: -pair[1])[:RECALL_NOTES]
    return [f"- {notes[id].text}" for id, _ in ranked if id in notes]


async def _nothing(value):
    return value


# How a classmate seems, by mneme-llm's face for her mood: said as a classmate would notice it.
SEEMS = {"great": ("in high spirits", "很开心"), "good": ("happy", "开心"), "ok": ("calm", "平静"),
         "worry": ("worried", "有点担心"), "sad": ("sad", "有点难过")}
RECENT = 2 * DAY  # a classmate's mood counts as today's this long after anything moved her


def classmates_line(book: "Book", persona: str, subject: str, language: str) -> str:
    """How the others seem today with this person, from their own files (no model call); "" when none has felt
    anything lately. Her file alone is hers to change: this only reads theirs, and never makes one."""
    now, seen = time.time(), []
    for name in book.known():
        if name == persona or not (book.folder / f"{name}.db").exists():
            continue
        state = book.open(name).mood.state(subject)
        if state.updated_at is not None and now - state.updated_at <= RECENT:
            seen.append((book.shown(name), SEEMS.get(state.face, (state.face, state.face))))
    if not seen:
        return ""
    if language.startswith("zh"):
        return "（" + "，".join(f"{name}今天看起来{word[1]}" for name, word in seen) + "。）"
    return "(" + "; ".join(f"{name} seems {word[0]} today" for name, word in seen) + ".)"


def impulse(trigger) -> str:
    """Her own motive when the message is a turn she started herself (an interface marks it `extra.speak_up`: a
    student's Discord bot speaking up), else ""."""
    return str((getattr(trigger, "extra", None) or {}).get("speak_up") or "")


def called(book: Book, persona: str, subject: str, owner: bool, sender) -> str:
    """What her memory calls the person, the most particular first: what they asked of her (`$callme <student>
    <name>`), `call_owner` set for her alone (the owner), what they asked of every student (`$callme <name>`),
    `call_owner` for all (the owner), their name in `people:`, the name they have where they wrote."""
    alone = str(book.settings.students.get(persona, {}).get("call_owner") or "")
    return (book.call(subject, persona) or (alone if owner else "") or book.call(subject)
            or (book.settings.call_owner if owner else "") or book.name_of(subject) or sender.name or sender.id)


def where(address) -> str:
    """The conversation an exchange was said in, as her memory names it."""
    return str(address)


async def hear(book: Book, settings: Settings, exchange, subject: str, said) -> list[str]:
    """What was said in a group chat, every student there heard: each classmate keeps the exchange word for word too.
    Returns the classmates who keep it."""
    heard = []
    for persona in getattr(exchange, "personas", ()):  # (contracts 1.9)
        own = settings.of(persona)
        if persona == exchange.persona.name or not book.keeps(persona) or not (own.learn and own.episodes):
            continue
        await book.open(persona, create=True).episodes.akeep(subject, said, conversation=where(exchange.address))
        heard.append(persona)
    return heard


def share(book: Book, settings: Settings, exchange, subject: str, notes, facts) -> list[str]:
    """What was said in a group chat (a conversation with several students), every student there heard: the notes
    one kept of it are kept in each classmate's file as well, as her own (source "heard"). A direct chat stays
    between the two. Returns the classmates who keep them."""
    heard = []
    for persona in getattr(exchange, "personas", ()):  # (contracts 1.9)
        own = settings.of(persona)
        if persona == exchange.persona.name or not book.keeps(persona) or not (own.learn and own.notes):
            continue
        memory = book.open(persona, create=True)
        for note in notes:
            memory.remember(subject, note.text, kind=note.kind, key=note.key, importance=note.importance,
                            feeling=note.feeling, happened_at=note.happened_at, source="heard", gloss=note.gloss)
        book.publish(facts, persona, subject)
        heard.append(persona)
    return heard


def make_part(book: Book, settings: Settings):
    async def text(context) -> str:
        """Who the person is to her, what they call her, her notes for this message, what was said before that bears
        on it (her first look), her mood's news (until she has replied) and how her classmates seem today; nothing
        for a routine run, or a person she has nothing on."""
        persona, trigger = context.persona, context.trigger
        engine = getattr(context, "engine", "")  # (contracts 1.9; an older core gives none)
        if trigger.origin != "person" or not book.keeps(persona.name) or not conversation(engine, settings):
            return ""
        book.met(persona)
        own = settings.of(persona.name)
        subject = book.subject(context.address.interface, trigger.sender.id)
        person = called(book, persona.name, subject, context.owner, trigger.sender)
        memory = book.open(persona.name)
        if memory is None:  # (nothing kept of anyone yet)
            return no_notes(person, own.language) if own.learn and own.notes else ""
        who = "\n".join(line for line in (memory.relations.line(subject, person),
                                          memory.aliases.line(subject, person)) if line)
        query = impulse(trigger) or trigger.text  # (speaking up, her notes for what moves her)
        notes, said = await asyncio.gather(  # (each waits on the same query's embedding)
            memory.memory.acontext(subject, query, user_name=person) if own.notes else _nothing(""),
            first_look(memory, subject, query, person, where(context.address), own) if EPISODES and own.said
            else _nothing(""))
        notes = (notes or no_notes(person, own.language)) if own.notes else ""
        # Her mood's news is told once: marked by the watcher once she has replied, so a step asked twice (or a run
        # replayed) shows it each time; without the watcher, at once.
        news = (memory.mood.news(subject, mark=not own.learn) or "") if own.mood else ""
        others = classmates_line(book, persona.name, subject, own.language) if own.classmates else ""
        return "\n\n".join(part for part in (who, notes, said, news, others) if part)
    return text


def writer(models, profile: str | None):
    """mneme-llm's own calls (judging the mood, writing notes, her impression), on her model or `writer_profile`."""
    async def complete(system: str, user: str) -> str:
        result = await models.generate(ModelRequest(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], profile=profile,
            effort="none", max_tokens=1500, timeout_s=60.0))  # short judgments: thinking cost several times the reply
        return result.text
    return complete


def first_role(memory, subject: str, own: Settings, owner: bool) -> None:
    """Who someone new is to her: the bot's owner her creator (`owner_role`), anyone else an acquaintance
    (`met_role`); your `roles:` say more. An owner's second account, not joined to the first, is no second creator."""
    for role in ([own.owner_role] if owner else []) + [own.met_role]:
        try:
            if role:
                memory.relate(subject, role)
                return
        except ValueError:  # (there is one creator)
            continue


def make_learner(book: Book, settings: Settings):
    async def learn(exchange, context) -> None:
        """After each of her messages: the exchange kept word for word, how it left her feeling and what is worth
        keeping (one decision call, and a note writer only when something is), a day of contact for her closeness,
        and once a day her impression. The exchange and the notes from a group chat go to her classmates there too
        (`hear`, `share`). A message she started herself (speaking up) moves her mood only: notes and what was said
        come from what people tell her."""
        persona, trigger = exchange.persona, exchange.trigger
        if trigger.origin != "person" or not conversation(exchange.engine, settings) or not book.keeps(persona.name):
            return
        own = settings.of(persona.name)
        if not own.learn:
            return
        book.met(persona)
        memory = book.open(persona.name, create=True)
        subject = book.subject(exchange.address.interface, trigger.sender.id)
        person = called(book, persona.name, subject, exchange.owner, trigger.sender)
        motive = impulse(trigger)
        if not motive and memory.relations.get(subject) is None:
            first_role(memory, subject, own, exchange.owner)
        complete = writer(context.models, own.writer_profile)
        memory.mood.news(subject, mark=True)  # she has replied: what moved her before was told
        said = [(f"({persona.display_name}'s own thought)", motive) if motive else (person, trigger.text),
                (persona.display_name, exchange.text)]
        keep = own.episodes and EPISODES and not motive
        kept = {"keep": keep, "conversation": where(exchange.address)} if EPISODES else {}
        if own.mood or own.notes:
            seen = await memory.aobserve(subject, said, source="speak-up" if motive else "user", user_name=person,
                                         complete=complete, mood=own.mood, capture=own.notes, **kept)
            if seen.error:
                context.log("notes_not_written", persona=persona.name, error=seen.error)
            if seen.notes:
                share(book, settings, exchange, subject, seen.notes, context.facts)
        elif not motive:
            memory.relations.seen(subject)
            if keep:
                await memory.episodes.akeep(subject, said, conversation=where(exchange.address))
        if keep:
            await hear(book, settings, exchange, subject, said)
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
            return ToolResult.fail(f"No student called {who!r} here (known: {shown}).", "unknown_student")
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


def make_recall(book: Book, settings: Settings, tool: str):
    """Her `search` or `read` through what was said before with the person who wrote, as mneme-llm's tools do it."""
    async def recall(arguments, context) -> ToolResult:
        persona = getattr(context, "persona", "")  # (contracts 1.14: whose turn called it)
        if not persona or not book.keeps(persona) or not settings.of(persona).search:
            return ToolResult.fail("Only a student's own turn can look through her memory.", "not_a_student")
        box = book.toolbox(persona, context.run_id)
        if box is None:
            return ToolResult.ok("Nothing said before is kept yet.")
        subject = book.subject(context.address.interface, context.sender.id)
        return ToolResult.ok(await box.acall(tool, dict(arguments), subject=subject))
    return recall


def publish_person(book: Book, facts, subject: str) -> None:
    """Every student's facts for this person, on each of their accounts (after a name or a link changed)."""
    for persona in book.known():
        book.publish(facts, persona, subject)


def make_callme(book: Book, facts=None):
    async def callme(arguments, context) -> ToolResult:
        """`$callme` shows it, `$callme <name>` asks every student, `$callme <student> <name>` one of them, and
        `reset` (for all or for her) takes the name back."""
        subject = book.subject(context.address.interface, context.sender.id)
        text = " ".join(str(arguments.get("text") or "").split())
        if not text:
            lines = [f"{book.shown(persona)}: {book.calling(persona, subject)['name'] or context.sender.name}"
                     for persona in book.known()]
            return ToolResult.ok("\n".join(["What the students call you:", *lines]) if lines else "No students yet.")
        who, _, rest = text.partition(" ")
        persona = book.find(who, subject) if rest else None
        name = rest if persona else text
        shown = book.shown(persona) if persona else "every student"
        if name.casefold() in RESET:
            book.set_call(subject, persona or "*", None)
            publish_person(book, facts, subject)
            return ToolResult.ok(f"Your name for {shown} is back to the usual." if persona
                                 else "The students call you as before.")
        if len(name) > NAME_MAX or not re.search(r"\w", name):
            return ToolResult.fail(f"A name of 1 to {NAME_MAX} characters, please.", "invalid_name")
        book.set_call(subject, persona or "*", name)
        publish_person(book, facts, subject)
        return ToolResult.ok(f"{shown[0].upper()}{shown[1:]} will call you {name}." if persona
                             else f"The students will call you {name}.",
                             data={"persona": persona, "name": name})
    return callme


def make_link(book: Book, facts=None):
    async def link(arguments, context) -> ToolResult:
        """`$link` gives a code (and lists your accounts); `$link <code>`, sent from another account of yours within
        ten minutes, makes the two one person to every student."""
        subject = book.subject(context.address.interface, context.sender.id)
        text = str(arguments.get("text") or "").strip()
        if not text:
            code = book.code_for(subject)
            accounts = book.accounts.get(book.person(subject), [subject])
            return ToolResult.ok(f"Your accounts: {', '.join(accounts)}. To add one, send `$link {code}` from it "
                                 f"(Discord, WeChat, another page) within ten minutes.", data={"code": code,
                                                                                        "accounts": accounts})
        person = book.redeem(text)
        if person is None:
            return ToolResult.fail("That code is wrong or too old: ask for a new one with `$link`.", "invalid_code")
        if book.person(subject) == person:
            return ToolResult.ok("This account is already yours there.")
        if not book.join(subject, person):
            return ToolResult.fail(f"{subject} belongs to someone already (`people:` in the students' settings says "
                                   "so, or other accounts are linked to it).", "taken")
        book.save()
        publish_person(book, facts, subject)
        return ToolResult.ok(f"Linked: {subject} and {person} are one person to every student now.",
                             data={"person": person, "account": subject})
    return link


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


def contributions(context, students: dict[str, dict]) -> dict:
    """The memory's prompt part, watcher, commands and facts, for these students (persona → display name and other
    names), or those of them `personas:` names."""
    config = dict(context.config)
    # Each student's own memory settings (her student.yml `memory:`), under this module's `students: <name>:`.
    given = dict(config.get("students") or {})
    config["students"] = {name: {**dict((students.get(name) or {}).get("memory") or {}), **dict(given.get(name) or {})}
                          for name in [*given, *(name for name in students if (students[name] or {}).get("memory"))]}
    settings = Settings(config, Path(context.data_dir))
    keep = tuple(name for name in settings.personas or students if name in students)
    settings.personas = keep
    book = Book(context.data_dir, personas=keep, platforms={**PLATFORMS, **dict(config.get("platforms") or {})},
                roles=config.get("roles"), people=config.get("people"), settings=settings)
    for name, names in students.items():  # her names, before she has spoken anywhere
        book.names.setdefault(name, names)
    facts = getattr(context, "facts", None)  # (None when loaded without a core)
    book.publish_all(facts)
    learns = settings.learn or any(flag(own.get("learn"), False) for own in settings.students.values())
    searching = tuple(name for name in keep if settings.of(name).search) if EPISODES else ()
    spec = {item["name"]: item for item in SPECS}
    return {
        "prompt_parts": [PromptPart("memory", make_part(book, settings), place="turn", order=50, personas=keep)],
        "watchers": [Watcher("memory", make_learner(book, settings), personas=keep, timeout_s=180)] if learns else [],
        "capabilities": [
            Capability("students.alias", "Show or set what you call a student.", make_alias(book, facts),
                       {"text": {"type": "string", "description": "<student> <name>, <student> reset, or nothing"}},
                       risk=Risk.WRITE, internal=True, command="alias", command_argument="text"),
            Capability("students.bond", "Where each student stands with you.", make_bond(book), risk=Risk.READ,
                       internal=True, command="bond"),
            Capability("students.callme", "Show or set what the students call you.", make_callme(book, facts),
                       {"text": {"type": "string", "description": "<name>, <student> <name>, reset, or nothing"}},
                       risk=Risk.WRITE, internal=True, command="callme", command_argument="text"),
            Capability("students.link", "Join another account of yours, with a code.", make_link(book, facts),
                       {"text": {"type": "string", "description": "a code from `$link`, or nothing for a new one"}},
                       risk=Risk.WRITE, internal=True, command="link", command_argument="text"),
            # Hers alone (offered to her turns only), about the person who wrote, never anyone else.
            *([Capability("students.search_memory", spec["search"]["description"],
                          make_recall(book, settings, "search"), spec["search"]["parameters"]["properties"],
                          required=("query",), risk=Risk.READ, personas=searching, timeout_s=60),
               Capability("students.read_memory", spec["read"]["description"], make_recall(book, settings, "read"),
                          spec["read"]["parameters"]["properties"], required=("id",), risk=Risk.READ,
                          personas=searching, timeout_s=60)] if searching else []),
        ],
        # Each changes only what is yours (your name for her, her name for you, your own accounts: a link needs a
        # code from the account it joins), and only you can type them (models are not offered them).
        "permissions": {"*": {"rules": {"students.alias": "auto", "students.callme": "auto", "students.link": "auto"}}},
    }
