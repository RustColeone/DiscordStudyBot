# `whale_chan`: a second Discord bot with its own engine, persona and model profile

鲸酱 (Whale-chan) is an external module: her own Discord application that chats like a personal AI
web client, one ongoing thread per DM or channel, with full Markdown answers, rendered math, image
understanding and the core's tools. The user-level description and the four setup steps are in
[plugins/README.md](../README.md) (section "The example: `whale_chan`"); this page is the developer
companion. Study it as the project's complete plugin: one folder that brings an interface, an engine, a
persona, a model profile and the defaults that make it work as soon as it is dropped in.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `whale_chan` | The Discord bot ([`bot.py`](bot.py)). Only when a token is set; the name comes from `interface` |
| Engine | `chat` | One persona; "a personal AI chat client" ([`engine.py`](engine.py)) |
| Persona | `whale_chan` | Display name `鲸酱`, the whale-girl prompt, profile `whale_chan` |
| Profile | `whale_chan` | `[{provider: deepseek, model: deepseek-flash}]`, or the `models` list |
| Assignment | `whale_chan:*` | `engine: chat`, `personas: [whale_chan]`, `budget: {model_calls: 30, tool_calls: 24, seconds: 1800}`, merged with `assignment` |
| Admission | `whale_chan:*` | `enabled: true`, `require_mention_in_groups: true` |
| Permissions | `whale_chan:*` | `tiers: {write: always-ask, critical: always-ask}` |
| Setting | `token` | `secret`, env `DISCORD_BOT_TOKEN_WHALE_CHAN`, checked by logging in to Discord |
| Setting | `display_name` | `text`, default `鲸酱` |
| Capability | `whale_chan.speak` | `prepare`: a short text spoken aloud as a voice message ([`voice.py`](voice.py)); off with `voice: {enabled: false}` |

Her memory and mood are kept by [mneme-llm](../../../mneme-llm/README.md), a standalone package (one SQLite file any
model can use) installed next to the core with `venv/bin/pip install -e ../mneme-llm`; without it she uses the
core's diary. The assignment, admission and permissions are suggestions: core.yml and runtime edits
win, setting by setting. Without a token, only the engine, persona and profile load (the log says
`no_token`), and you can assign them to any conversation:

```yaml
assignments:
  "discord:123": {engine: chat, personas: [whale_chan]}
```

## Setup

The steps (a Discord application with **Message Content Intent**, the token, a restart) are in
[plugins/README.md](../README.md). For the module's own settings, copy the example and keep the copy
private:

```
cp plugins/whale_chan/config.example.yml plugins/whale_chan/config.yml && chmod 600 plugins/whale_chan/config.yml
```

`extension_config.whale_chan` in core.yml, if present, is merged over this file.

| Key | Default | Meaning |
| --- | --- | --- |
| `token_env` | `DISCORD_BOT_TOKEN_WHALE_CHAN` | The environment variable holding the bot token |
| `token` | none | The token itself, instead of `.env` (then keep `config.yml` at `chmod 600` and never share the folder with it) |
| `interface` | `whale_chan` | Interface name; conversations become `<interface>:dm:<user id>` or `<interface>:<channel id>` |
| `display_name` | `鲸酱` | Her name |
| `prompt` | the whale-girl prompt | Replaces the personality ([`extension.py:40-49`](extension.py)) |
| `new_chat_text` | `🐳 好的主人，新的对话开始啦～` | The reply to `/new` |
| `effort` | `high` | Model effort: `low`, `medium` or `high` |
| `max_steps` | `10` | Model steps (tool rounds) per message |
| `max_tokens` | `8000` | Output tokens per model call |
| `history_limit` | `60` | Messages of the current thread sent as context |
| `history_chars` | `40000` | At most this many characters of them |
| `models` | DeepSeek Flash | The `whale_chan` profile: candidates tried in order; `vision: false` marks one that cannot see images |
| `slash_commands` | `help, memory, remind, reminders, routine, draw, search, read, video` | Core commands registered as slash commands besides `/new`; `[]` for none |
| `math` | `true` | Render display math as images |
| `math_width_px`, `math_dpi`, `math_font_size_pt`, `math_min_height_px`, `math_max_images` | 1440, 180, 20, 220, 10 | Image size and the most equations rendered per reply ([`math_render.py:128-141`](math_render.py)) |
| `assignment` | none | Keys merged over the default assignment (for example `options: {owner_instructions: "…"}`) |
| `permissions` | none | Read by the core, not by `setup`: `use` and `tiers` for her conversations (see the example file) |

Environment variables (names only): `DISCORD_BOT_TOKEN_WHALE_CHAN` (or the one `token_env` names), and
the key of each provider in `models`: the default profile needs `DEEPSEEK_API_KEY`, which the core's
built-in `deepseek` provider reads. Candidates whose provider has no key are skipped.
`python -m core setup` and `$setup` offer the token because it is a declared `secret` setting (only the
setup on the machine takes a secret), and `check_token` tries it before it is saved.

Packages: `discord.py` (from `requirements.txt`; it brings `aiohttp`, used for the token check).
`matplotlib` and `pylatexenc` are optional: without them display math is sent as text in a code block.

To turn it off, move the folder out or exclude it in core.yml:

```yaml
modules:
  exclude: [plugins/whale_chan]
```

## Using it

* **Where she answers.** Every DM. In servers, messages that mention her or reply to one of her
  messages (the mention is removed from the text). Other bots are ignored.
* **Threads.** A message that is exactly `/new`, `/reset` or `/clear` starts a fresh thread: earlier
  messages stay in history but stop being context. The `/new` slash command sends the same text.
* **Slash commands.** `/new`, plus the core commands named in `slash_commands` that exist in the
  catalog. They are registered globally at login, only when the list changed. Commands with a message
  action appear under Apps on a message. Typed `$` commands work too: in a DM directly, in a server
  with a mention (an owner's command needs none, since owners' commands skip admission).
* **Answers.** One progress line (`-# ⏳ Thinking…`, then what she is doing) is edited in place and
  deleted when the answer arrives; only the answer is posted, split at paragraph, line or word
  boundaries into 2,000-character messages. In servers the answer quotes the message it answers.
* **Approvals.** Every write and critical call asks the owner, with no exceptions (`always-ask`), in case
  she misread a request or a page tried to trick her. That includes a member's `$remind`; an owner's
  own typed command counts as the approval. Approval and other buttons arrive as Discord
  buttons; a press runs its command as the person who pressed it.
* **Images and files.** Attachments up to 25 MB on messages addressed to her are downloaded and passed
  to the core. Images are shown to the model (at most 6 per call); files from tools are sent back.
* **Limits.** `max_steps` per message, then she answers from what she has and says what remains
  unverified. The run budget is 30 model calls, 24 tool calls and 30 minutes.
* **Memory.** What she learns about each person (facts, preferences, plans, what happened, her own
  impressions) is kept in mneme-llm. After each reply, the same decision request that judges her mood asks
  whether anything was worth keeping; only then does a short call write the notes (at most 8 everyday notes a
  day), so her own turns carry no note-taking at all. Each message brings up the notes that match it in words or
  in meaning (OpenAI embeddings, falling back to word centroids learned from them, then to her model unifying
  the language), important facts and the newest episode, with dates as people say them ("yesterday", "in 2
  days"). A newer note with the same key ("user.city") replaces the older one, and the history keeps both.
  Once a day per person she rewrites her short impression of them. The first time she meets someone in
  mneme-llm, their core-diary notes are brought over. Inspect or fix it with
  `python -m mneme_llm --db … notes discord:<id>`.
* **Mood.** After each reply, how it left her feeling is judged, in the same request as the note gate:
  GPT-6 Luna's decisions when `OPENAI_API_KEY` is set (about 0.3 s and at most $0.0001), else the micro model once it
  has learned Luna's judgement, else one short call to her own model. Her mood (0–100, settling at 70) and her feelings
  (开心 踏实 甜蜜 心疼 担心 难过 吃醋) fade by themselves. She hears what moved her once, in her next prompt.
  It only drops from aching for you, worry or sadness, never from being cross with you.
* **Speaking up.** With `speak_up: {enabled: true}` she may message her owner first: a local dice roll
  every 30 minutes, likelier after a long quiet and off-peak, never in quiet hours (1–8), within 20
  minutes of her own message, or before you answered her last one. Her reason comes from memory (an exam
  you mentioned for tomorrow), a lingering feeling, or the time of day. It starts as a dry run that only
  writes down what it would have done. `/speakup status|rehearse|now|on|off|dry` (owner only).
* **Voice.** `/voice on` makes her short replies voice messages in that chat (`/voice off`, `/voice`
  shows which); her own speak-ups come as voice messages too. OpenAI speech (`gpt-4o-mini-tts`, voice
  `coral`, with a style instruction) by default; Piper, a local voice, is an optional backend
  ([`voice_piper.py`](voice_piper.py)), not installed by default and safe to delete. Code, links and math
  are never read aloud, and replies over 400 characters stay text.

## How it works

| File | Lines | Role |
| --- | --- | --- |
| [`extension.py`](extension.py) | 108 | Declaration, settings, token check, persona prompt; `setup` builds everything from `context.config` |
| [`engine.py`](engine.py) | 488 | `ChatEngine` (`chat`): the turn loop, thread context, images, memory, speak-up turns, voice and mood after a reply |
| [`bot.py`](bot.py) | 482 | `WhaleChanBot`: the Discord interface (incoming messages, slash commands, buttons, delivery, voice messages, the speak-up loop) |
| [`companion.py`](companion.py) | 117 | Her memory and mood in mneme-llm (optional; the engine falls back to the core diary) |
| [`speakup.py`](speakup.py) | 287 | Speaking up: the dice rules, motives and the ledger of who spoke when |
| [`voice.py`](voice.py) | 125 | `whale_chan.speak` and the OpenAI speech backend |
| [`voice_piper.py`](voice_piper.py) | 55 | Optional local Piper backend, loaded only with `voice: {backend: piper}` |
| [`slash.py`](slash.py) | 117 | Translating the core's `Command` catalog to Discord application commands and back; pending interactions |
| [`math_render.py`](math_render.py) | 169 | Inline math to Unicode, display math to PNG with Matplotlib |
| [`config.example.yml`](config.example.yml) | 109 | Documented settings to copy |
| `.gitignore` | 2 | Keeps `config.yml` out of git when the folder is its own repository |

**Setup** ([`extension.py:53-75`](extension.py)). `setup` reads `context.config`, builds the persona and
the engine, and always returns them with the profile. It looks for the token in `token`, then in the
variable `token_env` names; without one it logs and stops there. With one it adds the interface and its
defaults, all keyed by the interface's own name, because a module may only suggest `assignments`,
`admission`, and `use`/`tiers` permissions for its own interfaces.

**Incoming** ([`bot.py:124-154`](bot.py)). `on_message` submits every human message it sees with
`mentions_me` set from the platform's facts (a DM, a mention, a reply to her) and lets the core's
admission decide; the interface makes no policy decisions of its own. Request IDs are Discord message
IDs, so resubmits are dropped as duplicates. Slash commands are deferred at once ("thinking…"),
rendered to `$` text with `Command.render` and submitted the same way
([`bot.py:198-231`](bot.py)); `/new` becomes the plain text `/new` for the engine.

**The engine** ([`engine.py:199-243`](engine.py)). Each step asks the model for one JSON object matching
`TURN_SCHEMA`: `message`, `status` (`continue`, `complete`, `needs_input`, `blocked`), up to 5
`actions` and up to 2 `diary` notes. Invalid output or an invalid step (unknown tool, `continue`
without actions, actions with another status) gets up to two corrections ([`engine.py:468-529`](engine.py)).
On `continue` it saves the actions with call IDs `c1`, `c2`, … before calling them through
`session.tools.call`, shows `message` as progress, runs them in order, and stops the batch at the first
failure. `needs_approval` returns `WAIT_APPROVAL`; after the owner decides, `_absorb_events` reads the
`tool_result` or `approval_denied` event by index and continues ([`engine.py:351-445`](engine.py)).
`complete` and `blocked` reply and finish; `needs_input` waits for the next message.
Where her model can call tools itself (DeepSeek, OpenAI; `tool_calls: true`, the default), a step is her
words and function calls instead: the calls are the actions, `ask` waits for an answer, and with the core
diary `remember` keeps a note; each result goes back as its call's `tool` message, and a step whose model
cannot call tools is asked as JSON. Her final answer at the step limit and her speak-ups are always JSON.
The assistant engine's README ("Function calls") gives the details; this engine does the same.

**Context** ([`engine.py:531-629`](engine.py)). The system prompt is the persona prompt, her display
name, the current time, the assignment's `instructions`, `owner_instructions` (only when an owner is
speaking), the turn contract with the allowed tools, and her diary notes about the sender. The thread
is the conversation's history since the last `/new`, newest first within `history_limit` and
`history_chars`. Images come from the run (attachments, tool results) and from messages in the last
hour of the thread.

**Outgoing** ([`bot.py:242-302`](bot.py)). `deliver` posts text (rendered, split, with display math
turned into PNG images in a thread), files, notices in her own words (`NOTICES`) and the progress line.
For slash commands the first reply replaces the placeholder and later ones follow up; `private` replies
are ephemeral. It returns a `Delivery`: not sent for 403/404, uncertain for 5xx errors.

Design decisions:

* **The interface is optional.** The engine and persona are useful without the bot, so a missing token
  only skips the interface instead of failing the module.
* **Progress instead of speaking.** Unlike the built-in `assistant` engine, which speaks its message
  before running tools, `chat` shows it as progress and posts only the answer, like a web chat client.
* **A thread boundary, not a deletion.** `/new` stores the time in the persona's memory under
  `thread_since:<address>`; history is kept and only filtered.
* **Memory that outlives the model.** Notes go to mneme-llm, whose file any model or project can read, so
  switching her model keeps what she knows. They are written after the reply by a note writer
  (`memory.writer_profile`, her own model by default), not by her turn, so their quality does not depend on
  the chat model and her prompt carries no note-taking. `memory: {engine: core}` (or no mneme-llm) uses
  `session.diary` from the turn's `diary` field instead, where the core enforces the format and people manage
  notes with `$memory`. The assignment option `diary: false` turns both off. The notes and mood news are
  computed once per run, so every step and a replayed run see the same prompt.
* **After the reply, not before.** Voice and the mood judgement run once the answer is posted (a replayed
  run skips them), so she never answers slower; a message that arrives meanwhile starts the next run.
* **Speak-ups are hers.** Interfaces only submit requests for people, so a speak-up goes in her owner's
  name, marked in `extra.speak_up`, which only the bot's code sets; the bot strips the mark from anything
  typed, and the engine shows the turn to her as her own impulse, with no tools.
* **Per-conversation overrides.** `max_steps`, `history_limit`, `effort`, `new_chat_text`,
  `instructions`, `owner_instructions` and `diary` can be set in an assignment's `options`;
  `max_tokens` and `history_chars` only in the module's config.

Safety:

* Writes and critical actions always ask in her conversations; reads and prepares run.
* Owners: when core.yml lists none for her interface, she reports the Discord application's owner (or
  its team members) with `port.set_owners`, so approvals reach them without configuration.
* Replies never ping anyone (`AllowedMentions.none()`). Creator-only commands are hidden from server
  members (`default_member_permissions: "0"`); the core enforces access anyway.
* A rejected token or a missing Message Content Intent is reported with `port.set_ready(False, …)`,
  which tells the owner, instead of crashing the core.
* The token lives in `.env` by default. A `token` in `config.yml` is the owner's choice; the folder's
  `.gitignore` keeps that file out of git.

## Data it keeps

* `<data dir>/extensions/whale_chan/attachments/`: attachments of messages addressed to her, named
  `<message id>-<file name>`. The module never deletes them. (The core's data dir is `data/core` by
  default.)
* `<data dir>/extensions/whale_chan/math/`: rendered equations, deleted after they are sent.
* Her interface's bottle: `slash_fingerprint`, the hash of the registered commands.
* The `whale_chan` persona's memory: `thread_since:<address>` and `voice:<address>` per conversation.
* `<data dir>/extensions/whale_chan/mneme-llm.db`: her notes (with their vectors and glosses), profile cards,
  mood, mood timeline and the micro models, per person (`python -m mneme_llm --db … export` moves it anywhere).
* `<data dir>/extensions/whale_chan/speak_up.json`: when she and each person last wrote, unanswered
  speak-ups, the misses in a row, the last 50 rolls, and the owner's `/speakup` switches.
* Voice messages are written to the run's workspace (`voice/`), like other tool files.
* Kept by the core, not the module: conversation history, runs (the engine's state holds the step
  transcript), diary notes (at most 200 per persona and person; `$memory` reads, forgets or clears them).

## Tests

`tests/test_plugin_whale_chan.py`:

* `ChatEngineTests` (a real core with a fake model): tools run and only the answer is posted, at
  `high` effort, with the persona prompt; the thread lasts until `/new`; a long thread is trimmed to the
  character budget, newest first; at the step limit the model is told to answer.
* `WhaleChanSetupTests`: without a token only the engine, persona and profile load; the token comes
  from the environment or the local config; defaults follow a renamed `interface`; long text splits
  within 2,000 characters; the folder's `.gitignore` lists `config.yml`.
* `WhaleSlashTests`: her slash commands are `/new`, `/voice` and `/speakup` plus the catalog entries
  named in `slash_commands` (others, such as `call`, are left out); `/new` is submitted as text, and the
  reply edits the placeholder.
* `CompanionTests` (a real core): notes are written after the reply and recalled with dates, with no diary in
  her turn; dated plans; small talk never reaches the note writer; her impression is rewritten at most daily;
  the core diary when mneme-llm is off; the mood judged by her own model without Luna and told once; a
  speak-up is her impulse
  with no tools, and the mark typed by a person is an ordinary message; `/voice on` sends short replies
  as voice messages and never reads code aloud.
* `SpeakUpRuleTests`: the hit line (peak hours, timing, misses and their cap); quiet hours, cooldown and
  an unanswered message; dry runs only write down; the ledger survives restarts; motives from memory,
  feelings, silence and the clock.
* `WhaleBotExtrasTests`: only the owner steers speaking up; a voice message is measured (length and
  waveform) for Discord.

```
venv/bin/python -m unittest tests.test_plugin_whale_chan
```

`tests/test_core_boundaries.py` also checks that she imports only `contracts`, third-party packages
and her own folder.

## Building something similar

* **Contribute what works without the platform first**, and the interface only when it is configured
  ([`extension.py:62-75`](extension.py)). A module that every install gets but few use should do the same.
* **Declare settings with a check** ([`extension.py:26-38`](extension.py), [`extension.py:80-84`](extension.py)):
  `python -m core setup` and `$setup` then offer them, and a token is tried before it is saved.
* **Suggest defaults for your own addresses only**: `<interface>:*` assignments, admission and
  permissions ([`extension.py:69-75`](extension.py)). Anything else refuses to load.
* **A resumable engine**: state saved after each step, call IDs saved before calling, events tracked by
  index ([`engine.py:209-214`](engine.py), [`engine.py:351-445`](engine.py)). Compare with the built-in
  [`assistant`](../../extensions/assistant/README.md) engine.
* **Validate model output and correct it** instead of failing on the first bad step
  ([`engine.py:468-529`](engine.py)).
* **An interface that only translates**: platform facts in `Request` ([`bot.py:147-154`](bot.py)),
  owners from the platform ([`bot.py:111-121`](bot.py)), readiness ([`bot.py:87-102`](bot.py)), the
  command catalog as native commands ([`bot.py:181-196`](bot.py), [`slash.py:29-51`](slash.py)) and
  transport state in the bottle.
* **Keep blocking work off the event loop**: math is rendered with `asyncio.to_thread`
  ([`bot.py:316`](bot.py)).

Pitfalls:

* `permissions:` in her `config.yml` may set `use` and `tiers` only for her interface's addresses. With
  no token there is no interface, so such a section makes the core refuse to start; after renaming
  `interface`, rename the `"whale_chan:*"` keys too.
* The engine's prompt calls the display "Discord" whenever it shows Markdown, so it reads oddly when
  `chat` is assigned to another interface.
* The progress line has no Cancel button; use `$cancel`.

See also: [plugins/README.md](../README.md), [Writing modules](../../docs/core/EXTENSIONS.md) (in
particular [Layout and declaration](../../docs/core/EXTENSIONS.md#layout-and-declaration),
[Interfaces](../../docs/core/EXTENSIONS.md#interfaces), [Engines](../../docs/core/EXTENSIONS.md#engines)
and [Diaries](../../docs/core/EXTENSIONS.md#diaries)), [ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md),
the built-in [`discord`](../../extensions/discord/README.md) interface and
[`assistant`](../../extensions/assistant/README.md) engine, and the [`webui`](../webui/README.md) plugin.
