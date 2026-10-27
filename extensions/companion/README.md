# `companion`: what each student keeps of each person

The companion keeps, for every student, what she knows of each person, in [mneme-llm](https://github.com/RustColeone/mneme-llm):
one file per student. That is who the person is to her (a role, and how close they are), what they call her (an
alias), her notes about them and her mood toward them. Every engine that places prompt parts (the room, the
assistant) tells her the part that fits the message, each turn, without knowing this module. After each of her
messages she learns from it, whichever engine she spoke in, and the pages show where she stands with you.

It works for any persona from any module, by name, and imports none of them: "adding a companion is adding a
companion". Without it, the students still talk as before.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Prompt part | `companion` | Place `turn`, order 50, for every persona (or those in `personas:`), in the engines of `engines:`: "(Alice is your friend; you are close.)", "(Alice calls you Lulu.)", her notes for this message (found by its words, marked as data, not instructions) and her mood's news (told once). Nothing for a routine run or a person she has nothing on. |
| Watcher | `companion` | After each of her messages in a conversation engine (`engines:`): one decision call judges her mood and whether anything is worth keeping; only then does the note writer run. The first message of a day is a day of contact (closeness grows by 1, within the role's band). Someone new gets `met_role` (an acquaintance). Once a day, after `reflect_after_notes` notes, she rewrites her impression of the person. Then it publishes the facts below. Timeout 180 s; a failure is only audited. |
| Facts | `companion.alias`, `companion.bond`, `companion.mood` | Per student and person, for the pages (MomoChat shows them): your name for her; `{role, closeness}`; `{value, face, feelings}` (face: great, good, ok, worry or sad). Published after each exchange, after `$alias`, and at start for what the files already hold. |
| Capability | `companion.alias` | `$alias`: what you call each student; `$alias <student> <name>` names her, `$alias <student> reset` takes it back. The student is found by her name, her display name, her other names (Luna for GPT) or your alias for her. Typed only (models are not offered it); it runs without asking, since it changes only your own name for her. |
| Capability | `companion.bond` | `$bond`: each student's role, closeness and mood with you, and your name for her. |

It needs mneme-llm 0.2 or newer (`pip install -U mneme-llm`). Without it, the module adds nothing and records
`mneme_llm_missing` in the audit log.

**What learning costs.** Each message a student says in conversation is one decision call (her mood, and whether to
write a note), plus a note-writer call when there is something to keep, plus her impression once a day. They are
low-effort calls on her own model, or on `writer_profile`, counted in the run's usage (`watcher_calls`). `learn:
false` turns all of it off; `notes: false` or `mood: false` keeps half.

**Two note keepers.** The room keeps its own diary (notes the model adds inside its turn). With the companion's
notes on, a student has both; turn the room's off with `diary: false` in the room assignment's `options`. Whale-chan
keeps her own companion ([`plugins/whale_chan/companion.py`](../../plugins/whale_chan/companion.py)) until she moves
here: her engine (`chat`) is not in `engines:`, so she is neither learned nor told twice.

## Setup

The module loads by default. Its settings live in `extensions/companion/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml) and keep only what you change:

| Key | Default | Meaning |
| --- | --- | --- |
| `personas` | every persona | Whose companion it keeps. |
| `engines` | `[room, assistant]` | The engines whose messages are conversation. The Classroom's (`workbench`) are reports on work, which teach her nothing about you. |
| `learn` | true | Learn from what is said (the watcher). Off: only roles and aliases, as set. |
| `notes` | true | Keep notes, and give her the ones that fit each message. |
| `mood` | true | Judge her mood after each exchange, and tell her its news once. |
| `met_role` | `acquaintance` | The role someone gets the first time they talk with her, so closeness can grow (a person without a role has none). Empty: leave them without. |
| `writer_profile` | her own | The model profile for mneme-llm's own calls (a fixed cheap model, say). |
| `reflect_after_notes` | 5 | Her impression of a person is rewritten once a day, once she has this many notes. |
| `language` | mneme-llm's (`en`) | The language of the lines it adds (`en` or `zh`). The students' prompts are Chinese, so `zh` fits them. |
| `platforms` | none | Interfaces that share people, `{whale_chan: discord}`: a Discord user is one person on both bots. |
| `roles` | none | Who people are to every student, by subject (`"discord:<id>": creator`): `creator` (one), `family`, `friend` or `acquaintance`. A role is set by you, never by a model. |

A person is `<platform>:<sender id>`, the platform being the interface's name unless `platforms:` says otherwise.
Facts are published for each interface that platform covers (`discord` and `whale_chan`, say). A student is known
once she has spoken in a chat (her display name and other names are kept in `names.json`), or from `personas:`.

Each student's file is `data/extensions/companion/<persona>.db` (space `<persona>`), made the first time there is
something to keep. The `mneme-llm` command reads and changes it: `mneme-llm --db data/extensions/companion/gpt.db
--space gpt relations`.

## Does a student know the other students?

From her prompt, yes: the shared `school.md` names the club and each member, and her own prompt says how she sees
each of them. In a shared chat she also reads what the others said (the history). But her mneme-llm file holds
people only: she keeps no notes, mood or bond about a classmate, and does not know what another student knows about
you or how she feels today. Both could come from facts (another student's mood is a fact any page or part can read),
as a proposal: it changes what the students read.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `Settings` reads the config; `Book` opens each student's file when first needed, finds a student by name, says where she stands with someone (`stand`) and publishes it (`publish`, `publish_all`); `make_part` builds the turn part, `make_learner` the watcher (with `writer`, her model calls), `make_alias` and `make_bond` the commands; `setup` wires them in, with a permission rule letting `$alias` run without asking. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

The part reads the person's lines and changes nothing about them, except that the mood's news is told once. The
watcher sees each exchange after the reply is out (contracts 1.9, `Contributions.watchers`), one at a time per
student, so her file is never written twice at once.
