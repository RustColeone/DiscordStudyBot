# `companion`: what each student keeps of each person

The companion keeps, for every student, what she knows of each person, in [mneme-llm](https://github.com/RustColeone/mneme-llm):
one file per student. That is who the person is to her (a role, and how close they are), what they call her (an
alias), her notes about them, her mood toward them and their plans. Every engine that places prompt parts (the room,
the assistant, the chat engine of a student's own Discord bot) tells her the part that fits the message, each turn,
without knowing this module. After each of her messages she learns from it, whichever engine she spoke in, and the
pages show where she stands with you. However you reach a student (MomoChat, the room in Discord, a DM, her own
bot), she is one student with one memory of you; and you are one person to her on every account of yours that
`people:` names.

It works for any persona from any module, by name, and imports none of them: "adding a companion is adding a
companion". Without it, the students still talk as before.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Prompt part | `companion` | Place `turn`, order 50, for every persona (or those in `personas:`), in the engines of `engines:`: "(Alice is your friend; you are close.)", "(Alice calls you Lulu.)", her notes for this message (found by its words, marked as data, not instructions; "Notes about Alice: (none yet)" when there are none, so she knows her memory is kept for her) and her mood's news (until she has replied). Nothing for a routine run or a person she has nothing on. |
| Watcher | `companion` | After each of her messages in a conversation engine (`engines:`): one decision call judges her mood and whether anything is worth keeping; only then does the note writer run. In a group chat (a conversation with several students) the notes she keeps go into each classmate's file there too: they all heard it. The first message of a day is a day of contact (closeness grows by 1, within the role's band). Someone new gets `met_role` (an acquaintance). Once a day, after `reflect_after_notes` notes, she rewrites her impression of the person. Then it publishes the facts below. Timeout 180 s; a failure is only audited. |
| Facts | `companion.alias`, `companion.bond`, `companion.mood`, `companion.plans` | Per student and person, for the pages (MomoChat shows them) and speaking up (Whale-chan's bot): your name for her; `{role, closeness}`; `{value, face, feelings}` (face: great, good, ok, worry or sad); `[{text, at}]`, what you have coming from yesterday to a month ahead. Published for every account of the person, after each exchange, after `$alias`, and at start for what the files already hold. |
| Capability | `companion.alias` | `$alias`: what you call each student; `$alias <student> <name>` names her, `$alias <student> reset` takes it back. The student is found by her name, her display name, her other names (Luna for GPT) or your alias for her. Typed only (models are not offered it); it runs without asking, since it changes only your own name for her. |
| Capability | `companion.bond` | `$bond`: each student's role, closeness and mood with you, and your name for her. |

It needs mneme-llm 0.2 or newer (`pip install -U mneme-llm`). Without it, the module adds nothing and records
`mneme_llm_missing` in the audit log.

**What learning costs.** Each message a student says in conversation is one decision call (her mood, and whether to
write a note), plus a note-writer call when there is something to keep, plus her impression once a day. They are
low-effort calls on her own model, or on `writer_profile`, counted in the run's usage (`watcher_calls`). `learn:
false` turns all of it off; `notes: false` or `mood: false` keeps half.

**One note keeper.** The core diary is off in every engine (the room, the assistant, the chat engine; `diary: true`
in an assignment's `options`, or in an engine's settings, turns one back on), so what a student remembers of you is
kept here only, and is the same in every engine. Models are not offered the conversation notepad either
(`basic.remember` is typed only, `$remember`), which a student without her diary took for a memory of her own.

**Whale-chan is the DeepSeek student** (2026-10-27): her bot speaks as `deepseek`, so what she learns there is in
`deepseek.db` with what she learns in the room, under the same settings as every student. When a student speaks up
on her own (an interface marks the message `extra.speak_up`, as Whale-chan's bot does), the watcher treats it as her
own thought: her mood moves, no note is written about you, and she is not "seen" by you that day. Speaking up asks
about your `plans` and her `mood` from the facts.

**Group chats are heard by everyone there.** A note one student keeps from a conversation with several students
(the room in MomoChat or a Discord channel) is kept in each classmate's file as well, as her own (source `heard`):
she was there. A direct chat with one student stays between the two of you. Mood and closeness stay each student's
own, and only the one who answered has a day of contact.

**One person, several accounts.** `people: {Leo: ["discord:7067…", "webui:creator"]}` says that these accounts are
one person: every student's file links them (mneme-llm's `link`), so a note kept from Discord is there in the web
UI, her mood and bond are one, and the pages show the same on each. Their first account is where they are kept, and
the name is what her memory calls them (after `call_owner` for the owner). An account taken out of `people:` is its
own again from the next start; what was joined stays.

## Setup

The module loads by default. Its settings live in `extensions/companion/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml) and keep only what you change:

| Key | Default | Meaning |
| --- | --- | --- |
| `personas` | every persona | Whose companion it keeps. |
| `engines` | `[room, assistant, chat]` | The engines whose messages are conversation (`chat` is the engine of a student's own Discord bot). The Classroom's (`workbench`) are reports on work, which teach her nothing about you. |
| `learn` | true | Learn from what is said (the watcher). Off: only roles and aliases, as set. |
| `notes` | true | Keep notes, and give her the ones that fit each message. |
| `mood` | true | Judge her mood after each exchange, and tell her its news once. |
| `met_role` | `acquaintance` | The role someone gets the first time they talk with her, so closeness can grow (a person without a role has none). Empty: leave them without. |
| `writer_profile` | her own | The model profile for mneme-llm's own calls (a fixed cheap model, say). |
| `reflect_after_notes` | 5 | Her impression of a person is rewritten once a day, once she has this many notes. |
| `language` | mneme-llm's (`en`) | The language of the lines it adds (`en` or `zh`). The students' prompts are Chinese, so `zh` fits them. |
| `memory_language` | none | Notes are also kept in this language (a gloss each, written with the note), so a note is found by your words whatever language either was in (`Chinese`, `English`). |
| `decisions` | `core` | Who judges her mood and what is worth keeping: `core` (her own model, through the core) or `luna` (GPT-6 Luna's Decisions API with `OPENAI_API_KEY` from the environment, then the local micro model once it has learned, then her model). |
| `call_owner` | none | What her memory calls the bot's owner (`主人`), instead of their name. |
| `config` | none | A mneme-llm config file (TOML: prompts, sentences, roles, settings), read when her file is first made; relative to `data/extensions/companion/`. |
| `students` | none | Settings of one student's own, over these, for when one should differ: `students: {grok: {learn: false}}`. A student may set `learn`, `notes`, `mood`, `met_role`, `writer_profile`, `reflect_after_notes`, `language`, `memory_language`, `decisions`, `call_owner` and `config`. |
| `platforms` | none | Interfaces that share people, `{whale_chan: discord}`: a Discord user is one person on both bots. |
| `people` | none | One person behind several accounts, by name: `{Leo: ["discord:7067…", "webui:creator"]}` (the web UI's owner is `webui:creator`, its `user_id`). Every student keeps them as one; the first account is where, and the name is what her memory calls them. |
| `roles` | none | Who people are to every student, by subject (`"discord:<id>": creator`): `creator` (one), `family`, `friend` or `acquaintance`. A role is set by you, never by a model. |

A person is `<platform>:<sender id>`, the platform being the interface's name unless `platforms:` says otherwise,
or the first account `people:` names for them. Facts are published for each of their accounts, on each interface
its platform covers (`discord` and `whale_chan`, say). A student is known
once she has spoken in a chat (her display name and other names are kept in `names.json`), or from `personas:`.

Each student's file is `data/extensions/companion/<persona>.db` (space `<persona>`), made the first time there is
something to keep. Notes are found by their words (and their glosses): her part is put together without waiting on
the network, so mneme-llm's embeddings are not used. The `mneme-llm` command reads and changes it: `mneme-llm --db data/extensions/companion/gpt.db
--space gpt relations`.

## Does a student know the other students?

From her prompt, yes: the shared `school.md` names the club and each member, and her own prompt says how she sees
each of them. In a shared chat she also reads what the others said (the history), and keeps what was said there,
whoever answered (group chats above). But her mneme-llm file holds people only: she keeps no notes, mood or bond
about a classmate, and does not know what you told another student alone or how another feels today. Proposals
(DISCUSSION section 9f): classmates' moods in her part, bonds between students.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `Settings` reads the config; `Book` opens each student's file when first needed (with the links of `people:`, `link`), finds a student by name, says where she stands with someone (`stand`) and publishes it for each account (`publish`, `publish_all`); `make_part` builds the turn part, `make_learner` the watcher (with `writer`, her model calls, and `share`, a group chat's notes for the classmates there), `make_alias` and `make_bond` the commands; `setup` wires them in, with a permission rule letting `$alias` run without asking. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

The part reads the person's lines and changes nothing about them. Her mood's news is shown until she has replied:
the watcher marks it told then, so a step asked twice (or a run replayed after a restart) shows it each time. The
watcher sees each exchange after the reply is out (contracts 1.9, `Contributions.watchers`), one at a time per
student; a classmate's notes from a group chat may land in her file between two of her own (each write is one
transaction). It knows a group chat by the exchange's `personas`, every student taking part.
