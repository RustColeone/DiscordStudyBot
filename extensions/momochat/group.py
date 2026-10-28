"""Who speaks in a group chat (a conversation with several personas): the room's rules.

A message for everyone ("you all") is answered by each of them; one that calls on some of them ("Luna, ...",
"..., Luna?", "@Luna") by those it calls on, while others it names only in passing may add their word after; one that
names people without calling on anyone ("Can DeepSeek check this?") is for all it names. A message naming no one is
answered by whose turn it is (a rotation kept per conversation), and, with `chime_in`, the others may join in.
"""

from __future__ import annotations

import re

PLURAL = re.compile(r"你[们們]|大家|各位|\b(?:you all|you guys|all of you|everyone|everybody|the team)\b", re.I)
SELECTIVE = re.compile(r"你[们們](?:不用|不必|不需要|不要)|(?:不用|不必|不需要|不要).{0,8}(?:你[们們]|大家|各位)")
QUOTED = re.compile(r'```.*?```|"[^"\n]*"|“[^”\n]*”|「[^」\n]*」', re.S)
# What may stand around names when someone calls on them ("Hey Luna and Gemini, ..."), and where that call ends.
CALLING = re.compile(r"^(?:hey|hi|hello|ok|okay|so|and|or|&|yo|dear|嘿|哈喽|你好|和|跟|还有|或)$", re.I)
CALL_END = re.compile(r"[,，:：!！?？。.;；\n]")


def patterns(persona, aliases: dict[str, list[str]]) -> list[re.Pattern]:
    """Her names, the other names she is called by (Persona.aliases: "Luna" for GPT) and the engine's own."""
    names = {persona.name, persona.display_name, *getattr(persona, "aliases", ()), *aliases.get(persona.name, [])}
    return [re.compile(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", re.I) for name in names if name]


def participants(personas, text: str, aliases: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Who the message is for, as (addressed, mentioned)."""
    spoken = QUOTED.sub("", text)
    if PLURAL.search(spoken) and not SELECTIVE.search(spoken):
        return [persona.name for persona in personas], []
    named = [persona for persona in personas if any(name.search(spoken) for name in patterns(persona, aliases))]
    # A call is a stretch between punctuation of names and calling words only, wherever it stands: "Luna, Gemini,
    # what do you think?", "Luna, check v1.2. Gemini, check v1.3.", "..., Luna?"
    segments = CALL_END.split(spoken)

    def calls(segment: str, persona) -> bool:
        if not any(name.search(segment) for name in patterns(persona, aliases)):
            return False
        rest = segment
        for other in named:
            for name in patterns(other, aliases):
                rest = name.sub(" ", rest)
        return all(CALLING.match(word) for word in re.split(r"[\s@]+", rest) if word)

    addressed = [persona.name for persona in named if any(calls(segment, persona) for segment in segments)
                 or any(re.search(rf"@{name.pattern}", spoken, re.I) for name in patterns(persona, aliases))]
    if not addressed:
        return [persona.name for persona in named], []
    return addressed, [persona.name for persona in named if persona.name not in addressed]


def find(personas, name: str, aliases: dict[str, list[str]]):
    """The persona a name stands for (her name, display name or another name of hers), or None."""
    wanted = str(name or "").strip()
    for persona in personas:
        if wanted.lower() in {persona.name.lower(), persona.display_name.lower()} or any(
                pattern.fullmatch(wanted) for pattern in patterns(persona, aliases)):
            return persona
    return None
