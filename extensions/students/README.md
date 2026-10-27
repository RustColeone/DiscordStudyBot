# `students`: the characters, one folder each

The students are the characters every engine can use: GPT, Gemini, Claude, DeepSeek and Grok. This module says who
each one is (her prompt, display name, other names and picture) and which models she prefers. It runs nothing
itself. The room
([`extensions/room`](../room)) is where they usually talk, but any engine can use any of them by name.

Add a student by adding a folder. Change one by editing her files. The prompts are plain text files, so a change
to a character is a change to a file, not to code.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Persona | `gpt` | Display name `GPT`, profile `gpt`; also called `Luna`; formerly `luna` |
| Persona | `gemini` | Display name `Gemini`, profile `gemini` |
| Persona | `claude` | Display name `Claude`, profile `claude`; also called `Haiku` |
| Persona | `deepseek` | Display name `DeepSeek`, profile `deepseek`; also called `鲸酱`; formerly `whale_chan`, `whale` |
| Persona | `grok` | Display name `Grok`, profile `grok` |
| Provider | `grok` | `openai_compatible` at `https://api.x.ai/v1`, key in `GROK_API_KEY`, `schema_mode: native`, `effort_param: true`; `$model` offers models released since 2026-06-01, excluding IDs matching `imagine` |
| Setting | `grok_api_key` | `secret`, env `GROK_API_KEY`: "API key for Grok (`grok`; without it, GPT's model answers for her)" |

Every persona has her `avatar` picture (`<name>/avatar.png`), which every page shows through the core.

The model profiles, each tried in order (a candidate whose provider has no key is skipped):

| Profile | Candidates |
| --- | --- |
| `gpt` | `openai/gpt-6-luna`, `deepseek/deepseek-flash`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |
| `gemini` | `gemini/gemini-3.8-flash`, `gemini/gemini-3.6-flash`, `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `claude/claude-haiku-5-5` |
| `claude` | `claude/claude-haiku-5-5`, `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `gemini/gemini-3.8-flash` |
| `deepseek` | `deepseek/deepseek-flash`, `openai/gpt-6-luna`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |
| `grok` | `grok/grok-4-fast`, then GPT's chain: `openai/gpt-6-luna`, `deepseek/deepseek-flash`, `gemini/gemini-3.8-flash`, `claude/claude-haiku-5-5` |

Each student prefers her own models, then falls back to the others' first models, so any one key is enough for every
student. The `deepseek`, `openai`, `gemini` and `claude` providers are built into the core
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
order: 1                    # where she stands in lists (the room's default rotation follows assignments)
models:                     # her own models, preferred in this order
  - {provider: openai, model: gpt-6-luna}
fallback: 2                 # optional: her first model is everyone else's 2nd fallback
falls_back_to: gpt          # optional, instead: after her own models, this student's whole list
providers: {...}            # optional: a provider she brings, in the core.yml `providers:` form
pictures:                   # optional: files in her folder, for the pages (avatar: square; card: 3:4; any kind)
  avatar: avatar.png
prompt:                     # her prompt: these files in order (default: [prompt.md])
  - prompt.md
  - ../shared/school.md
```

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
personas:
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
| `profiles` | none | Replaces whole profiles by name (`gpt`, `grok`, ...). |

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
here now: move them from `extensions/room/config.yml` to `extensions/students/config.yml` unchanged. The room
ignores them and records `settings_moved` in the audit log if they are still there.

To turn the module off, add it to `modules: {exclude: [students]}` in `core.yml`. Its personas and profiles are then
gone too, and `python -m core --check` reports any assignment still naming them.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION`, with a key setting for each provider a student brings. `read_students` reads the folders, `compose` builds each prompt, `pictures` finds her picture files (inside her folder only), `profiles` builds the fallback lists, and `setup` merges `config.yml`'s `providers` and `profiles` over them. |
| `<name>/student.yml` | One student: name, display name, other names, former names, order, models, fallback, providers, pictures, prompt files. |
| `<name>/*.md`, [`shared/`](shared) | The prompt text. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

The folders are read once, when the module is imported, so a change to a student takes a restart.
