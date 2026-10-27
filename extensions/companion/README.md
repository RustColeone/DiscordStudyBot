# `companion`: what each student keeps of each person

The companion keeps, for every student, what she knows of each person, in [mneme-llm](https://github.com/RustColeone/mneme-llm):
one file per student. Today that is who the person is to her (a role) and what they call her (an alias). Every
engine that places prompt parts (the room, the assistant) tells her both, each turn, without knowing this module.

It works for any persona from any module, by name, and imports none of them: "adding a companion is adding a
companion". Without it, the students still talk as before.

**Not yet:** learning from what is said (notes, mood, closeness growing on each day of contact). A module cannot see
delivered messages until the core has watchers (proposed in DISCUSSION section 9d), so Whale-chan keeps her own
companion ([`plugins/whale_chan/companion.py`](../../plugins/whale_chan/companion.py)) until then.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Prompt part | `companion` | Place `turn`, order 50, for every persona (or those in `personas:`). "(Alice is your friend; you are close.)" and "(Alice calls you Lulu.)"; nothing for a routine run or a person she has nothing on. |
| Capability | `companion.alias` | `$alias`: what you call each student; `$alias <student> <name>` names her, `$alias <student> reset` takes it back. The student is found by her name, her display name or your alias for her. Typed only (models are not offered it); it runs without asking, since it changes only your own name for her. |
| Capability | `companion.bond` | `$bond`: each student's role and closeness with you, and your name for her. |

It needs mneme-llm 0.2 or newer (`pip install -U mneme-llm`). Without it, the module adds nothing and records
`mneme_llm_missing` in the audit log.

## Setup

The module loads by default. Its settings live in `extensions/companion/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml) and keep only what you change:

| Key | Default | Meaning |
| --- | --- | --- |
| `personas` | every persona | Whose companion it keeps. |
| `language` | mneme-llm's (`en`) | The language of the lines it adds (`en` or `zh`). The students' prompts are Chinese, so `zh` fits them. |
| `platforms` | none | Interfaces that share people, `{whale_chan: discord}`: a Discord user is one person on both bots. |
| `roles` | none | Who people are to every student, by subject (`"discord:<id>": creator`): `creator` (one), `family`, `friend` or `acquaintance`. A role is set by you, never by a model. |

A person is `<platform>:<sender id>`, the platform being the interface's name unless `platforms:` says otherwise.
A student is known once she has spoken in a chat (her display name is kept in `names.json`), or from `personas:`.

Each student's file is `data/extensions/companion/<persona>.db` (space `<persona>`), made the first time there is
something to keep. The `mneme-llm` command reads and changes it: `mneme-llm --db data/extensions/companion/luna.db
--space luna relations`.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `Book` opens each student's file when first needed and finds a student by name; `make_part` builds the turn part, `make_alias` and `make_bond` the commands; `setup` wires them in, with a permission rule letting `$alias` run without asking. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

The part reads the person's lines (`relations.line`, `aliases.line`) and changes nothing about them, so rendering a
prompt twice is the same as once; it only notes the student's display name, for `$alias`.
Notes and the mood's news are left out on purpose: the room keeps its own diary today, and both would say the same
thing twice. They move here with the watchers, and the room's diary is then turned off with `diary: false`.
