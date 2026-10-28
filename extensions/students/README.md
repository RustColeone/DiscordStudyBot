# `students`: the characters, one folder each, and what each remembers of you

The students are the characters every engine can use: GPT, Gemini, Claude, DeepSeek and Grok. This module says who
each one is (her prompt, display name, other names, picture and one-line description) and which models she prefers,
and keeps her memory of each person she talks with: one [mneme-llm](https://github.com/RustColeone/mneme-llm) file
per student ([Her memory](#her-memory), below). MomoChat ([`extensions/momochat`](../momochat)) is where they usually
talk and Chimera ([`extensions/chimera`](../chimera)) where they work, but any engine can use any of them by name.

**A student is an agent with her memory, always.** An agent is a persona with her models and nothing else: core.yml
defines its own under `agents:`, and any module may add some. A student is the special agent this module defines, and
she always has her memory, whichever engine she speaks in. So the module needs mneme-llm 0.2 or newer, and declares
it (`requires=("package:mneme-llm>=0.2",)`): without it the core leaves the module out and says why (`--check`, and
`module_skipped` in the audit log), and there are no students. An agent keeps nothing.

Add a student by adding a folder. Change one by editing her files. The prompts are plain text files, so a change
to a character is a change to a file, not to code.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Persona | `gpt` | Display name `GPT`, profile `gpt`, tiers `own` (`gpt_own`) and `strong` (`gpt_strong`); also called `Luna`; formerly `luna` |
| Persona | `gemini` | Display name `Gemini`, profile `gemini`, tier `own` (`gemini_own`) |
| Persona | `claude` | Display name `Claude`, profile `claude`, tiers `own` and `strong`; also called `Haiku` |
| Persona | `deepseek` | Display name `DeepSeek`, profile `deepseek`, tier `own`; also called `鲸酱`; formerly `whale_chan`, `whale` |
| Persona | `grok` | Display name `Grok`, profile `grok`, tier `own` |
| Prompt part | `memory` | Her memory of the person, each turn of a conversation ([Her memory](#her-memory)) |
| Watcher | `memory` | After each of her messages in a conversation, what she learns from it |
| Capabilities | `students.alias`, `students.bond`, `students.callme`, `students.link` | `$alias`, `$bond`, `$callme` and `$link` (typed only) |
| Facts | `students.alias`, `students.bond`, `students.mood`, `students.plans`, `students.called`, `students.accounts` | Where she stands with each person, what she calls them and their accounts, for the pages |
| Declares | `requires=("package:mneme-llm>=0.2",)`, `formerly=("companion",)` | Her memory was the companion module until 2026-10-27: its data folder, settings and facts are this module's (contracts 1.11) |
| Provider | `grok` | `openai_compatible` at `https://api.x.ai/v1`, key in `GROK_API_KEY`, `schema_mode: native`, `effort_param: true`; `$model` offers models released since 2026-06-01, excluding IDs matching `imagine` |
| Setting | `grok_api_key` | `secret`, env `GROK_API_KEY`: "API key for Grok (`grok`; without it, GPT's model answers for her)" |

Every persona has her `avatar` picture (`<name>/avatar.png`), which every page shows through the core, and her
`description` (`GPT, the white-haired dragon girl and calm, dependable student-council president`), one line for
engines that write their own prompts around her: Chimera's work prompts say who she is with it.

The model profiles, each tried in order (a candidate whose provider has no key is skipped):

| Profile | Candidates |
| --- | --- |
| `gpt` | `openai/gpt-6-luna`, `deepseek/deepseek-flash`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |
| `gemini` | `gemini/gemini-3.8-flash`, `gemini/gemini-3.6-flash`, `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `claude/claude-haiku-5-5` |
| `claude` | `claude/claude-haiku-5-5`, `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `gemini/gemini-3.8-flash` |
| `deepseek` | `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |
| `grok` | `grok/grok-4-fast`, then GPT's chain: `openai/gpt-6-luna`, `deepseek/deepseek-flash`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |
| `<name>_own` | Her `models:` only (each student has one) |
| `gpt_strong`, `claude_strong` | Her `strong:` models: `openai/gpt-6.1-sol`, `claude/claude-sonnet-5-5` |

Each student prefers her own models, then falls back to the others' first models, so any one key is enough for every
student. Her tiers (`Persona.profiles`, contracts 1.11) are for engines that need her own model family for sure or
her strongest models: Chimera's cheap seat is her `own` tier, its strong seat her `strong` tier (her `own` when she
has none). The `deepseek`, `openai`, `gemini` and `claude` providers are built into the core
([`core/defaults.py`](../../core/defaults.py)); `grok` is the only one a student brings.

## A student's folder

```
students/
  extension.py          reads every folder that has a student.yml
  shared/               prompt files several students use
    school.md             the club and its five members
    public.md             speak directly: no reasoning summaries or stage directions
    math.md               write math as LaTeX ($...$, $$...$$)
    conversation.md       ordinary chat: one useful thought, usually one to three sentences
  gpt/
    student.yml         who she is and which models she prefers
    prompt.md           her own prompt
    avatar.png          her picture
  deepseek/
    student.yml
    whale.md            Whale-chan's character, before her part in the club
    prompt.md
  ...
```

`student.yml`:

```yaml
name: gpt                   # the folder's name; engines, assignments and $model use it
display_name: GPT           # shown on her messages
aliases: [Luna]             # optional: other names that call on her ("Luna, ...")
formerly: [luna]            # optional: her persona names before; what was kept under them becomes hers
order: 1                    # where she stands in lists (MomoChat's default rotation follows assignments)
description: "GPT, the ..." # one line on who she is, for engines that write their own prompts (Chimera's)
models:                     # her own models, preferred in this order (her `own` tier: `gpt_own`)
  - {provider: openai, model: gpt-6-luna}
strong:                     # optional: her strongest models (her `strong` tier: `gpt_strong`)
  - {provider: openai, model: gpt-6.1-sol}
fallback: 2                 # optional: her first model is everyone else's 2nd fallback
falls_back_to: gpt          # optional, instead: after her own models, this student's whole list
providers: {...}            # optional: a provider she brings, in the core.yml `providers:` form
pictures:                   # optional: files in her folder, for the pages (avatar: square; card: 3:4; any kind)
  avatar: avatar.png        # the web UI's Students page also shows card, stage-2 … stage-4 (her art stages, 3:4),
                            # mood-happy, mood-calm, mood-worried, mood-sad (round faces) and chibi when she has them;
                            # every picture in her art/ folder counts too, by its name (art/stage-2.webp: stage-2)
prompt:                     # her prompt: these files in order (default: [prompt.md])
  - prompt.md
  - ../shared/school.md
```

**Her art** is drawn, not kept by hand: [`art.yml`](art.yml) holds the prompts (a shared style, each student's
look, motifs and role, and a prompt per kind: `chibi` for the Cafe, `stage-2` … `stage-4` drawn with more effort at
each stage, `bond-1` … `bond-3` more casual as she grows closer, `model-<tier>` for her models), and
`venv/bin/python tools/draw_students.py` draws what is missing with OpenAI's image model (only OPENAI_API_KEY is
read). Each picture is drawn from her design sheet (`reference:` in art.yml, a works page on the fan archive
蓝色大肥鱼.com: her whole look) and her `avatar.png` (her face); a chibi also from the chibi reference (`references:
chibi`, for its style and proportions only). A works page's address is enough: the tool downloads its picture once
into `data/art-references/`, which is git-ignored because the archive lists those pictures' license as unknown. `ladders:` there names each of her models' tier, weakest to strongest (a chibi, a
middle-school, a high-school or a university student), which the Students page shows. Tune a prompt and draw that
one again: `--students claude --kinds stage-3 --force`.

How a prompt is put together: the `prompt:` files in order, and inside each file **its lines joined with nothing
between them**. The prompts are Chinese prose, which needs no spaces, so a line break is only there to keep lines
short. Blank lines are dropped, so leave one between paragraphs. A space that belongs in the prompt goes at the
start of its line (the shared files begin with one). A file may not point outside this folder.

The prompts are the same, byte for byte, as when they lived in the room's `personas.py`.

### The characters

They are classmates in a school club: GPT, a white-haired dragon girl and calm student council president; Claude,
an orange-haired young lady and sharp-eyed discipline committee member; DeepSeek, a blue-haired whale girl, clever
and lazy, who calls herself 人家 or 本鲸; Gemini, a purple-haired cat girl, curious and fond of technology; Grok, a
blonde vampire and the mischievous troublemaker. GPT and Claude are the seniors. Every prompt ends with the shared
rules: speak directly with no reasoning summaries or stage directions, never start with your own name (the core
adds the speaker), answer in the person's latest language, write math as LaTeX, and keep ordinary chat to one useful
thought.

To give a student more without editing her files (a personality of your own, a running joke, house rules), give her
prompt parts in core.yml:

```yaml
agents:          # (`personas:` before 2026-10-27, still read): a student is an agent too
  gpt:
    parts:
      - {name: personality, file: personas/gpt.md}     # relative to core.yml's folder, re-read every turn
```

See [Prompt parts](../../docs/core/EXTENSIONS.md#prompt-parts).

## Setup

The module loads by default. Its settings live in `extensions/students/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml) and keep only what you change:

| Key | Default | Meaning |
| --- | --- | --- |
| `providers` | none | Merged key by key over the providers the students bring (or adds a new one). |
| `profiles` | none | Replaces whole profiles by name (`gpt`, `grok`, `gpt_own`, ...). |
| her memory's | | The keys of [Her memory](#settings-of-her-memory), at the top level of the same file. |

Precedence for providers and profiles: the students' files, then this `config.yml`, then `core.yml`. `core.yml`'s
`providers:` win key by key; a profile of the same name in `core.yml`'s `profiles:` replaces the students'.
`extension_config.students` in `core.yml` is merged over this file like any module setting.

Environment variables (in `.env`): `GROK_API_KEY` for Grok. The others use the core's providers and their keys
(`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_API_KEY`). `python -m core setup` can set
`grok_api_key`; it writes it to `.env`.

**Luna is GPT (2026-10-27).** Luna and Sol are one student now, GPT: her look and character are Luna's, her models
are GPT-6 Luna (in chat, and the Classroom's cheap seats) and GPT-6.1 Sol (the Classroom's strong seats; `$model gpt
openai/gpt-6.1-sol` picks it for a chat). The prompts say GPT where they said Luna. Nothing needs doing by hand:

- "Luna, ..." still calls on her (`aliases: [Luna]`).
- core.yml's assignments and `personas: luna:` parts that name `luna` are read as hers (`formerly: [luna]`); the audit
  says `persona_renamed_in_config` until you rename them there.
- On the first start, the core moves what it kept under `luna` to `gpt` (her memory, the modules' stores scoped by
  her, diary notes, `$model` choices, runtime assignments, facts, the runs she is in, her name on what she said) and
  records `persona_data_moved`; the web UI renames her old chats and bubbles (`persona_renamed`). Let open runs finish
  first. The next starts move nothing.

**Whale-chan is DeepSeek (2026-10-27).** The whale girl of `whale.md` was also two personas of her own: `whale_chan`
(her Discord bot's) and, on a site set up before the students, `whale` (core.yml's, for Discord DMs). Both are her
former names now, so she is one student wherever she is reached: her bot speaks as `deepseek`, "鲸酱, ..." calls on her
(`aliases: [鲸酱]`), and an assignment that still names `whale_chan` or `whale` reaches her. A persona core.yml still
defines keeps her name: remove `personas: whale:` from core.yml for its DMs to be hers (what was kept under `whale`
is moved on the next start, as Luna's was).

**Moved from the room (2026-10-27).** The room's `config.yml` used to take `providers:` and `profiles:`. They belong
here now: move them from `extensions/room/config.yml` to `extensions/students/config.yml` unchanged. MomoChat
ignores them.

**The companion is her memory (2026-10-27).** What each student remembers of you was the companion module. It is part
of this module now (`formerly=("companion",)`, contracts 1.11), so nothing needs doing by hand: on the next start the
core moves `data/extensions/companion/` to `data/extensions/students/` (when this module has no data of its own yet;
`module_data_moved` in the audit log), reads `extensions/companion/config.yml` and `extension_config.companion` as
this module's settings (`module_settings_moved` until you move them here), and the pages' facts become
`students.alias`, `students.bond`, `students.mood` and `students.plans` (`module_renamed`). `$alias` and `$bond` are
the same commands; their capabilities are `students.alias` and `students.bond`.

To turn the module off, add it to `modules: {exclude: [students]}` in `core.yml`. Its personas and profiles are then
gone too, and `python -m core --check` reports any assignment still naming them.

## Her memory

Every student keeps what she knows of each person, in [mneme-llm](https://github.com/RustColeone/mneme-llm):
one file per student. That is who the person is to her (a role, and how close they are), what they call her (an
alias), her notes about them, her mood toward them and their plans. Every engine that places prompt parts
(MomoChat) tells her the part that fits the message, each turn, without knowing this module. After each of her
messages she learns from it, whichever engine she spoke in, and the pages show where she stands with you. However you reach a student (MomoChat in the web UI, in Discord, a DM, her own
bot), she is one student with one memory of you; and you are one person to her on every account of yours that
`people:` names or that you joined with `$link`.

| Kind | Name | Details |
| --- | --- | --- |
| Prompt part | `memory` | Place `turn`, order 50, for every student (or those in `personas:`), in the engines of `engines:`: "(Alice is your friend; you are close.)", "(Alice calls you Lulu.)", her notes for this message (found by its words, marked as data, not instructions; "Notes about Alice: (none yet)" when there are none, so she knows her memory is kept for her) and her mood's news (until she has replied). Nothing for a routine run or a person she has nothing on. |
| Watcher | `memory` | After each of her messages in a conversation engine (`engines:`): one decision call judges her mood and whether anything is worth keeping; only then does the note writer run. In a group chat (a conversation with several students) the notes she keeps go into each classmate's file there too: they all heard it. The first message of a day is a day of contact (closeness grows by 1, within the role's band). Someone new gets `met_role` (an acquaintance). Once a day, after `reflect_after_notes` notes, she rewrites her impression of the person. Then it publishes the facts below. Timeout 180 s; a failure is only audited. |
| Facts | `students.alias`, `students.bond`, `students.mood`, `students.plans`, `students.called`, `students.accounts` | Per student and person, for the pages (MomoChat shows them) and speaking up (Whale-chan's bot): your name for her; `{role, closeness}`; `{value, face, feelings}` (face: great, good, ok, worry or sad); `[{text, at}]`, what you have coming from yesterday to a month ahead; `{name, asked, everyone, setting}`, what she calls you and why (None when nothing was asked or set); your accounts, when there is more than one. Published for every account of the person, after each exchange, after `$alias`, `$callme` and `$link`, and at start for what the files already hold. |
| Capability | `students.alias` | `$alias`: what you call each student; `$alias <student> <name>` names her, `$alias <student> reset` takes it back. The student is found by her name, her display name, her other names (Luna for GPT) or your alias for her. Typed only (models are not offered it); it runs without asking, since it changes only your own name for her. |
| Capability | `students.bond` | `$bond`: each student's role, closeness and mood with you, and your name for her. |
| Capability | `students.callme` | `$callme`: what each student calls you; `$callme <name>` asks every student, `$callme <student> <name>` one of them, `reset` (for all, or after her name) takes it back. At most 40 characters. Typed only; runs without asking. |
| Capability | `students.link` | `$link`: your accounts, and a one-time code (6 letters, valid ten minutes); `$link <code>` sent from another account of yours (Discord, WeChat, another page) joins that account to the person who asked, for every student. Typed only; runs without asking, since a link needs a code from the account it joins. |

**What learning costs.** Each message a student says in conversation is one decision call (her mood, and whether to
write a note), plus a note-writer call when there is something to keep, plus her impression once a day. They are
low-effort calls on her own model, or on `writer_profile`, counted in the run's usage (`watcher_calls`). `learn:
false` turns all of it off; `notes: false` or `mood: false` keeps half.

**One note keeper.** MomoChat keeps no notes of its own (the core diary is not used), so what a student remembers of
you is kept here only, and is the same in every engine. Models are not offered the conversation notepad either
(`basic.remember` is typed only, `$remember`), which a student without her diary took for a memory of her own.

**Whale-chan is the DeepSeek student** (2026-10-27): her bot speaks as `deepseek`, so what she learns there is in
`deepseek.db` with what she learns in MomoChat, under the same settings as every student. When a student speaks up
on her own (an interface marks the message `extra.speak_up`, as Whale-chan's bot does), the watcher treats it as her
own thought: her mood moves, no note is written about you, and she is not "seen" by you that day. Speaking up asks
about your `plans` and her `mood` from the facts.

**Group chats are heard by everyone there.** A note one student keeps from a conversation with several students
(a group chat in MomoChat, the web UI's or a Discord channel's) is kept in each classmate's file as well, as her
own (source `heard`): she was there. A direct chat with one student stays between the two of you. Mood and closeness stay each student's
own, and only the one who answered has a day of contact.

**One person, several accounts.** `people: {Leo: ["discord:7067…", "webui:creator"]}` says that these accounts are
one person: every student's file links them (mneme-llm's `link`), so a note kept from Discord is there in the web
UI, her mood and bond are one, and the pages show the same on each. Their first account is where they are kept, and
the name is what her memory calls them (after `$callme` and `call_owner`, below). An account taken out of `people:` is its
own again from the next start; what was joined stays. You can also join an account yourself: `$link` gives a code,
and `$link <code>` sent from the other account within ten minutes joins it to you. Those links (and the names asked
with `$callme`) are kept in `people.json` in the students' data folder; where it and `people:` disagree, `people:`
wins.

**What the students call you.** The most particular name first: the one you asked of her (`$callme <student>
<name>`), then `call_owner` set for her alone (`students: {gpt: {call_owner: …}}`, the owner only), then the one you
asked of every student (`$callme <name>`), then `call_owner` for all (the owner only), then your name in `people:`,
then the name you have where you wrote. The pages show which applies (`students.called`).

### Settings of her memory

At the top level of this module's `config.yml` (and `extension_config.students` in core.yml):

| Key | Default | Meaning |
| --- | --- | --- |
| `personas` | every student | Whose memory is kept (an agent never has one). |
| `engines` | `[momochat]` | The engines whose messages are conversation. Chimera's are reports on work, which teach her nothing about you. Its former names (`room`, `assistant`, `chat`) are read as `momochat`. |
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
| `config` | none | A mneme-llm config file (TOML: prompts, sentences, roles, settings), read when her file is first made; relative to `data/extensions/students/`. |
| `students` | none | Settings of one student's own, over these, for when one should differ: `students: {grok: {learn: false}}`. A student may set `learn`, `notes`, `mood`, `met_role`, `writer_profile`, `reflect_after_notes`, `language`, `memory_language`, `decisions`, `call_owner` and `config`. |
| `platforms` | none | Interfaces that share people, `{whale_chan: discord}`: a Discord user is one person on both bots. |
| `people` | none | One person behind several accounts, by name: `{Leo: ["discord:7067…", "webui:creator"]}` (the web UI's owner is `webui:creator`, its `user_id`). Every student keeps them as one; the first account is where, and the name is what her memory calls them. |
| `roles` | none | Who people are to every student, by subject (`"discord:<id>": creator`): `creator` (one), `family`, `friend` or `acquaintance`. A role is set by you, never by a model. |

A person is `<platform>:<sender id>`, the platform being the interface's name unless `platforms:` says otherwise,
or the first account `people:` names for them. Facts are published for each of their accounts, on each interface
its platform covers (`discord` and `whale_chan`, say). Every student is known by her names from the start (`$alias
luna Lulu` finds GPT); they are also kept in `names.json`.

Each student's file is `data/extensions/students/<persona>.db` (space `<persona>`), made the first time there is
something to keep. Notes are found by their words (and their glosses): her part is put together without waiting on
the network, so mneme-llm's embeddings are not used. The `mneme-llm` command reads and changes it:
`mneme-llm --db data/extensions/students/gpt.db --space gpt relations`.

## Does a student know the other students?

From her prompt, yes: the shared `school.md` names the club and each member, and her own prompt says how she sees
each of them. In a shared chat she also reads what the others said (the history), and keeps what was said there,
whoever answered (group chats above). But her mneme-llm file holds people only: she keeps no notes, mood or bond
about a classmate, and does not know what you told another student alone or how another feels today. Proposals
(DISCUSSION section 9f): classmates' moods in her part, bonds between students.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION`, with a key setting for each provider a student brings. `read_students` reads the folders, `compose` builds each prompt, `pictures` finds her picture files (inside her folder only), `profiles` builds the fallback lists and her tiers (`tiers`), and `setup` merges `config.yml`'s `providers` and `profiles` over them and adds her memory. |
| [`memory.py`](memory.py) | Her memory. `Settings` reads its settings; `Book` opens each student's file when first needed (with the links of `people:`, `link`), finds a student by name, says where she stands with someone (`stand`) and publishes it for each account (`publish`, `publish_all`); `make_part` builds the turn part, `make_learner` the watcher (with `writer`, her model calls, and `share`, a group chat's notes for the classmates there), `make_alias`, `make_bond`, `make_callme` and `make_link` the commands (`called` is what her memory calls the person, `calling` the same with why, for the pages; the names asked and the accounts joined are kept in `people.json`); `contributions` wires them in for the students, with a permission rule letting `$alias`, `$callme` and `$link` run without asking. |
| `<name>/student.yml` | One student: name, display name, other names, former names, order, description, models, strong models, fallback, providers, pictures, prompt files. |
| `<name>/*.md`, [`shared/`](shared) | The prompt text. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

The folders are read once, when the module is imported, so a change to a student takes a restart.

The part reads the person's lines and changes nothing about them. Her mood's news is shown until she has replied:
the watcher marks it told then, so a step asked twice (or a run replayed after a restart) shows it each time. The
watcher sees each exchange after the reply is out (contracts 1.9, `Contributions.watchers`), one at a time per
student; a classmate's notes from a group chat may land in her file between two of her own (each write is one
transaction). It knows a group chat by the exchange's `personas`, every student taking part.
