"""A test engine that keeps the core diary (no engine of this repository does since 2026-10-27: a student's memory
is the students module's, an agent keeps nothing; the core still offers the diary to any engine that wants one).

  note <key> <importance> <text>   write a note about the sender ("noted", or "refused: <why>")
  anything else                     reply with the notes recalled for it ("notes: a; b", or "notes: none")
"""

from contracts import Contributions, Engine, EngineSpec, Extension, RunYield


class Diarist(Engine):
    spec = EngineSpec("diarist", "test engine with a diary", "one")

    async def run(self, session):
        diary = session.diary(session.personas[0].name)
        text = session.trigger.text
        if text.startswith("note "):
            _, key, importance, note = text.split(" ", 3)
            try:
                diary.write(key, "fact", note, int(importance))
                await session.reply("noted")
            except ValueError as error:
                await session.reply(f"refused: {error}")
            return RunYield.DONE
        notes = diary.recall(text, 8)
        await session.reply("notes: " + ("; ".join(note.note for note in notes) or "none"))
        return RunYield.DONE


EXTENSION = Extension("diarist", lambda context: Contributions(engines=[Diarist()]), contracts="1")
