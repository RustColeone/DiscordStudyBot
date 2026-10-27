"""Companion extension: what each student keeps of each person, in mneme-llm (one file per student).

Every engine that places prompt parts (the room, the assistant) tells her, each turn, who the person is to her and
what they call her: `$alias` sets that name, `$bond` shows where each student stands with you. It works for any
persona from any module, by name, and imports none of them.

Not yet: learning from what is said (notes, mood, closeness growing on each day of contact). A module cannot see
delivered messages until the core has watchers (DISCUSSION section 9d), so Whale-chan keeps her own companion until
then.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from contracts import Capability, Contributions, Extension, PromptPart, Risk, ToolResult

try:
    import mneme_llm
except ImportError:  # optional: without it the module adds nothing
    mneme_llm = None

RESET = {"reset", "remove", "clear", "none"}


def usable() -> bool:
    """mneme-llm installed, and new enough to keep aliases (0.2)."""
    return mneme_llm is not None and hasattr(mneme_llm, "Aliases")


def key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


class Book:
    """Every student's mneme-llm file, `<data>/<persona>.db` with the space `<persona>`, opened when first needed.

    A file is made only when there is something to keep: an alias, or a role from the settings."""

    def __init__(self, folder: Path, *, personas=(), platforms=None, roles=None, language: str = ""):
        self.folder, self.personas = Path(folder), tuple(personas)
        self.platforms = {str(name): str(platform) for name, platform in (platforms or {}).items()}
        self.roles = {str(subject): str(role) for subject, role in (roles or {}).items()}
        self.language = language
        self.opened: dict[str, object] = {}
        self.index = self.folder / "names.json"  # persona → display name, as engines have shown them
        try:
            self.names = dict(json.loads(self.index.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            self.names = {}

    def keeps(self, persona: str) -> bool:
        return not self.personas or persona in self.personas

    def subject(self, interface: str, sender_id: str) -> str:
        """One person, whichever bot they talk to: `<platform>:<id>` (`whale_chan` counts as `discord`, say)."""
        return f"{self.platforms.get(interface, interface)}:{sender_id}"

    def met(self, persona: str, display_name: str) -> None:
        if self.names.get(persona) != display_name:
            self.names[persona] = display_name
            self.folder.mkdir(parents=True, exist_ok=True)
            self.index.write_text(json.dumps(self.names, ensure_ascii=False, indent=1), encoding="utf-8")

    def known(self) -> list[str]:
        found = {path.stem for path in self.folder.glob("*.db")} | set(self.names) | set(self.personas)
        return sorted(persona for persona in found if self.keeps(persona))

    def find(self, name: str, subject: str = "") -> str | None:
        """The student a name stands for: her persona name, her display name, or what this person calls her."""
        wanted = key(name)
        for persona in self.known():
            if wanted in (key(persona), key(self.names.get(persona, ""))):
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
        memory = mneme_llm.Mneme(str(path), persona, companion_name=self.names.get(persona, persona),
                                 display_language=self.language or mneme_llm.STORED, luna=False, micro=False,
                                 embed=False, meaning_micro=False, memory_language=None)
        for subject, role in self.roles.items():
            memory.relate(subject, role)  # (the same role again changes nothing)
        self.opened[persona] = memory
        return memory

    def close(self) -> None:
        for memory in self.opened.values():
            memory.close()
        self.opened.clear()


def make_part(book: Book):
    def text(context) -> str:
        """Who the person is to her, and what they call her (nothing for a routine run, or a stranger)."""
        persona, trigger = context.persona, context.trigger
        if trigger.origin != "person" or not book.keeps(persona.name):
            return ""
        book.met(persona.name, persona.display_name)
        memory = book.open(persona.name)
        if memory is None:
            return ""
        subject = book.subject(context.address.interface, trigger.sender.id)
        person = trigger.sender.name or trigger.sender.id
        return "\n".join(line for line in (memory.relations.line(subject, person),
                                           memory.aliases.line(subject, person)) if line)
    return text


def make_alias(book: Book):
    async def alias(arguments, context) -> ToolResult:
        subject = book.subject(context.address.interface, context.sender.id)
        text = str(arguments.get("text") or "").strip()
        if not text:
            names = {persona: memory.aliases.get(subject) for persona in book.known()
                     if (memory := book.open(persona)) is not None}
            lines = [f"{book.names.get(persona, persona)}: {name}" for persona, name in names.items() if name]
            return ToolResult.ok("\n".join(["What you call them:", *lines]) if lines else
                                 "You have not named anyone yet. `$alias <student> <name>` gives one a name of yours.")
        who, _, name = text.partition(" ")
        persona = book.find(who, subject)
        if persona is None:
            shown = ", ".join(book.names.get(persona, persona) for persona in book.known()) or "nobody yet"
            return ToolResult.fail(f"No student called {who!r} here (known: {shown}). A student is known once she has "
                                   "spoken in a chat, or from `personas:` in the companion's config.yml.",
                                   "unknown_student")
        shown, name = book.names.get(persona, persona), name.strip()
        memory = book.open(persona, create=True)
        if not name:
            present = memory.aliases.get(subject)
            return ToolResult.ok(f"You call {shown} {present}." if present else f"You have no name for {shown} yet.")
        if name.casefold() in RESET:
            memory.alias(subject, None)
            return ToolResult.ok(f"{shown} is {shown} to you again.")
        try:
            stored = memory.alias(subject, name)
        except ValueError as error:
            return ToolResult.fail(str(error), "invalid_alias")
        return ToolResult.ok(f"You call {shown} {stored} now; she will know.", data={"persona": persona, "alias": stored})
    return alias


def make_bond(book: Book):
    async def bond(arguments, context) -> ToolResult:
        subject = book.subject(context.address.interface, context.sender.id)
        lines = []
        for persona in book.known():
            memory = book.open(persona)
            relationship = memory.relations.get(subject) if memory is not None else None
            alias = memory.aliases.get(subject) if memory is not None else None
            stand = (f"{relationship.role}, closeness {relationship.closeness:.0f}/100" if relationship
                     else "no role yet")
            lines.append(f"{book.names.get(persona, persona)}: {stand}" + (f"; you call her {alias}" if alias else ""))
        return ToolResult.ok("\n".join(lines) or "No students yet.")
    return bond


def setup(context) -> Contributions:
    if not usable():
        context.log("mneme_llm_missing", hint="pip install -U mneme-llm (0.2 or newer) for the companion")
        return Contributions()
    config = context.config
    book = Book(context.data_dir, personas=config.get("personas") or (), platforms=config.get("platforms"),
                roles=config.get("roles"), language=str(config.get("language") or ""))
    return Contributions(
        prompt_parts=[PromptPart("companion", make_part(book), place="turn", order=50,
                                 personas=tuple(config.get("personas") or ()))],
        capabilities=[
            Capability("companion.alias", "Show or set what you call a student.", make_alias(book),
                       {"text": {"type": "string", "description": "<student> <name>, <student> reset, or nothing"}},
                       risk=Risk.WRITE, internal=True, command="alias", command_argument="text"),
            Capability("companion.bond", "Where each student stands with you.", make_bond(book), risk=Risk.READ,
                       internal=True, command="bond"),
        ],
        # Naming a student changes only your own name for her, and only you can type it (models are not offered it).
        permissions={"*": {"rules": {"companion.alias": "auto"}}})


EXTENSION = Extension("companion", setup, contracts="1", version="1",
                      description="What each student keeps of each person (mneme-llm): roles and aliases")
