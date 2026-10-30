# `whale_chan`: a second Discord bot for the DeepSeek student, on MomoChat

鲸酱 (Whale-chan) is an external module: her own Discord application that chats like a personal AI
web client, one ongoing thread per DM or channel, with full Markdown answers, rendered math, image
understanding, the core's tools and her voice. She is the DeepSeek student (the whale girl, 本鲸): the bot is one
more way to reach her, beside MomoChat's other pages, so her prompt, name, models and what she remembers of you are
the [students module's](../../extensions/students/README.md), the same wherever you talk with her. The conversation
itself is [MomoChat's](../../extensions/momochat/README.md): the bot is a frontend of it, and the module brings an
interface, her voice and the defaults that make it work as soon as it is dropped in. The user-level description
and the four setup steps are in [plugins/README.md](../README.md) (section "The example: `whale_chan`"); this page is
the developer companion. Study it as the project's complete plugin.

Until 2026-10-27 the module had an engine of its own, `chat`. That engine is MomoChat now, in its `full` style:
`chat` is MomoChat's former name, so a core.yml that still says `engine: chat` reaches it, and her old engine
settings here are her chats' MomoChat options.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `whale_chan` | The Discord bot ([`bot.py`](bot.py)), which also speaks her short replies as voice messages. Only when a token is set; the name comes from `interface` |
| Assignment | `whale_chan:*` | `engine: momochat`, `personas: [deepseek]` (the `persona` setting), `budget: {model_calls: 30, tool_calls: 24, seconds: 1800}`, `options:` a chat client's (`style: full`, `effort: high`, `max_steps: 10`, `max_tokens: 8000`, `history_limit: 60`, `history_chars: 40000`, `timeout_s: 240`, `compact: false`, her `new_chat_text`), merged with `assignment` |
| Admission | `whale_chan:*` | `enabled: true`, `require_mention_in_groups: true` |
| Permissions | `whale_chan:*` | `tiers: {write: always-ask, critical: always-ask}` |
| Setting | `token` | `secret`, env `DISCORD_BOT_TOKEN_WHALE_CHAN`, checked by logging in to Discord |
| Setting | `persona` | `text`, default `deepseek`: the student her bot speaks as |
| Declares | `requires=("students", "momochat")` | Her bot is a student's, on MomoChat: without either the core leaves this module out and says why (contracts 1.10) |
| Capability | `whale_chan.speak` | `prepare`: a short text spoken aloud as a voice message ([`voice.py`](voice.py)), for her model to call ("say goodnight to me"); off with `voice: {enabled: false}` |

She is nothing of her own here: no engine, no persona, no model profile, no memory. Her prompt (`whale.md`,
`prompt.md` and the shared parts), her display name, the alias 鲸酱 and her models are in
`extensions/students/deepseek/`; the persona was called `whale_chan` before (and the live site's DM persona
`whale`), and both names are her `formerly`, so a core.yml that still says `whale_chan` reaches her, and what was
kept under it is hers. Her memory and mood are the students module's, as every student's are: one mneme-llm file,
`deepseek.db`, whether you talk with her here, in a Discord DM of the main bot or in a group chat; with
`platforms: {whale_chan: discord}` in the students' settings, one Discord user is one person on both bots. The
assignment, admission and permissions are suggestions: core.yml and runtime edits win, setting by setting. Without
a token only her voice capability loads (the log says `no_token`).

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
| `persona` | `deepseek` | The student her bot speaks as (her prompt, name and models are that student's; [prompt parts](../../docs/core/EXTENSIONS.md#prompt-parts) in core.yml add to them: `agents: deepseek: parts:`) |
| `new_chat_text` | `🐳 好的主人，新的对话开始啦～` | The reply to `/new` |
| `effort` | `high` | Model effort: `low`, `medium` or `high` |
| `max_steps` | `10` | Model steps (tool rounds) per message |
| `max_tokens` | `8000` | Output tokens per model call |
| `history_limit` | `60` | Messages of the current thread sent as context |
| `history_chars` | `40000` | At most this many characters of them |
| `tool_calls` | `true` | Function calls where her model can make them (false: JSON steps only) |
| `slash_commands` | `help, memory, remind, reminders, routine, draw, search, read, video` | Core commands registered as slash commands besides `/new`; `[]` for none |
| `math` | `true` | Render display math as images |
| `math_width_px`, `math_dpi`, `math_font_size_pt`, `math_min_height_px`, `math_max_images` | 1440, 180, 20, 220, 10 | Image size and the most equations rendered per reply ([`math_render.py:128-141`](math_render.py)) |
| `assignment` | none | Keys merged over the default assignment (for example `options: {owner_instructions: "…"}`) |
| `permissions` | none | Read by the core, not by `setup`: `use` and `tiers` for her conversations (see the example file) |

`new_chat_text` to `tool_calls` are her chats' MomoChat options: the assignment she suggests carries them, and an
assignment's own `options` win. Her old settings `display_name`, `prompt`, `models`, `call_owner`, `memory`, `mood`
and `diary` are read no more: the audit log says `settings_moved`, and where each now lives (`diary`: nowhere, a
student's memory is the students module's), while they are still in this config.yml.

Environment variables (names only): `DISCORD_BOT_TOKEN_WHALE_CHAN` (or the one `token_env` names), and
the key of each provider in her student's models: DeepSeek's need `DEEPSEEK_API_KEY`, which the core's
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
* **Threads.** A message that is exactly `/new`, `/reset` or `/clear` starts a fresh thread (MomoChat's): earlier
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
* **Memory and mood** (the students module's, as every student's). What she learns about each person (facts,
  preferences, plans, what happened, her own impressions) and how each reply left her feeling are kept in the
  DeepSeek student's mneme-llm file, `data/extensions/students/deepseek.db`, whichever way you reach her. After
  each reply one decision request judges her mood and whether anything was worth keeping; only then does a short
  call write the notes. Each message brings up the notes that match its words (and their glosses in
  `memory_language`), with dates as people say them. Her mood (0–100, settling at 70) and feelings fade by
  themselves; she hears what moved her in her next prompt. The settings are the students module's
  (`extensions/students/config.yml`), the same for every student.
* **Speaking up.** With `speak_up: {enabled: true}` she may message her owner first: a local dice roll
  every 30 minutes, likelier after a long quiet and off-peak, never in quiet hours (1–8), within 20
  minutes of her own message, or before you answered her last one. Her reason comes from her memory's facts (a
  plan of yours for tomorrow, from its `plans`; a lingering feeling, from her `mood`), or the time of day. It starts as a dry run that only
  writes down what it would have done. `/speakup status|rehearse|now|on|off|dry` (owner only), or typed as
  `$speakup now` on any interface.
* **Voice.** `/voice on` (or `$voice on`) makes her short replies voice messages in that chat (`/voice off`, `/voice`
  shows which; the bot answers these itself, and keeps the choice in its bottle); her own speak-ups come as voice
  messages too, and an answer for which she already sent one herself (`whale_chan.speak`) gets no second. OpenAI speech (`gpt-4o-mini-tts`, voice
  `coral`, with a style instruction) by default; Piper, a local voice, is an optional backend
  ([`voice_piper.py`](voice_piper.py)), not installed by default and safe to delete. Code, links and math
  are never read aloud, and replies over 400 characters stay text.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | Declaration, settings, token check, the settings that moved, her chats' MomoChat options (`OPTIONS`, `chat_options`); `setup` builds everything from `context.config` |
| [`bot.py`](bot.py) | `WhaleChanBot`: the Discord interface (incoming messages, slash commands, buttons, delivery, `/voice` and voice messages after her answers, the speak-up loop) |
| [`speakup.py`](speakup.py) | Speaking up: the dice rules, motives and the ledger of who spoke when |
| [`voice.py`](voice.py) | `whale_chan.speak` and the OpenAI speech backend, which the bot also uses for her answers |
| [`voice_piper.py`](voice_piper.py) | Optional local Piper backend, loaded only with `voice: {backend: piper}` |
| [`slash.py`](slash.py) | Translating the core's `Command` catalog to Discord application commands and back; pending interactions |
| [`math_render.py`](math_render.py) | Inline math to Unicode, display math to PNG with Matplotlib |
| [`config.example.yml`](config.example.yml) | Documented settings to copy |
| `.gitignore` | Keeps `config.yml` out of git when the folder is its own repository |

**Setup** ([`extension.py`](extension.py)). `setup` reads `context.config`, reports settings that moved, and
always returns her voice capability. It looks for the token in `token`, then in the variable `token_env` names;
without one it logs and stops there. With one it adds the interface and its defaults (the assignment names the
`persona` student and MomoChat, with her chats' options), all keyed by the interface's own name, because a module
may only suggest `assignments`, `admission`, and `use`/`tiers` permissions for its own interfaces.

**Incoming** ([`bot.py`](bot.py), `on_message`). It submits every human message it sees with `mentions_me` set
from the platform's facts (a DM, a mention, a reply to her) and lets the core's admission decide; the interface
makes no policy decisions of its own. Request IDs are Discord message IDs, so resubmits are dropped as duplicates.
Slash commands are deferred at once ("thinking…"), rendered to `$` text with `Command.render` and submitted the
same way; `/new` becomes the plain text `/new` for MomoChat; `/voice` and `/speakup` the bot answers itself.

**The conversation** is MomoChat's ([its README](../../extensions/momochat/README.md)), in the `full` style: her
notes while she works are the progress line, only the answer is posted, at high effort, one thread until `/new`.
Her speak-ups are MomoChat turns from her own impulse (`extra.speak_up`), with no tools.

**Outgoing** ([`bot.py`](bot.py), `deliver`). It posts text (rendered, split, with display math turned into PNG
images in a thread), files, notices in her own words (`NOTICES`) and the progress line. For slash commands the first
reply replaces the placeholder and later ones follow up; `private` replies are ephemeral. Then, for a short answer
in a chat with `/voice on` or one to her own speak-up, it says it aloud too (`_speak`: the voice backend in a
thread, then a Discord voice message), once per run, and not when the run already sent a voice file of hers. It
returns a `Delivery`: not sent for 403/404, uncertain for 5xx errors.

Design decisions:

* **A frontend, not an engine.** How a conversation runs is MomoChat's; the bot renders it for Discord. Voice is a
  rendering of her answer, so it is the bot's too, and needs no engine to know about it.
* **The interface is optional.** A missing token only skips the interface instead of failing the module.
* **A way to reach a student, not a student.** Who she is lives with the other students, with what she remembers,
  so the bot adds no second Whale-chan who knows less.
* **Speak-ups are hers.** Interfaces only submit requests for people, so a speak-up goes in her owner's name,
  marked in `extra.speak_up`, which only the bot's code sets; the bot strips the mark from anything typed, and
  MomoChat shows the turn to her as her own impulse, with no tools.

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
* `<data dir>/extensions/whale_chan/voice/`: her answers spoken aloud, deleted once sent.
* Her interface's bottle: `slash_fingerprint`, the hash of the registered commands, and `voice:<conversation>`, on
  or off.
* Her memory and mood: the students module's `<data dir>/extensions/students/deepseek.db`.
* `<data dir>/extensions/whale_chan/speak_up.json`: when she and each person last wrote, unanswered
  speak-ups, the misses in a row, the last 50 rolls, and the owner's `/speakup` switches.
* Voice messages her model asks for (`whale_chan.speak`) are written to the run's workspace (`voice/`), like other
  tool files.
* Kept by the core and MomoChat, not the module: conversation history, runs, where each thread began (`/new`).

## Tests

`tests/test_plugin_whale_chan.py`:

* `ChatEngineTests`, `PromptLayoutTests`, `NativeChatTests` (a real core with a fake model, the students module and
  her chats' MomoChat options, assigned as `chat`): tools run and only the answer is posted, at `high` effort, with
  the DeepSeek student's prompt; the thread lasts until `/new`; a long thread is trimmed to the character budget,
  newest first; at the step limit the model is told to stop and answer; each turn reuses the last one's prefix; by
  function calls she asks and keeps no diary; her last step and a speak-up are JSON; an approval answers its call.
* `WhaleChanSetupTests`: without a token only her voice loads; her old settings are reported as moved; the token
  comes from the environment or the local config, the assignment is MomoChat with the DeepSeek student and her old
  engine settings as its options; defaults follow a renamed `interface`; long text splits within 2,000 characters;
  the folder's `.gitignore` lists `config.yml`.
* `WhaleSlashTests`: her slash commands are `/new`, `/voice` and `/speakup` plus the catalog entries named in
  `slash_commands`; `/new` is submitted as text, and the reply edits the placeholder.
* `CompanionTests` (a real core with the students' memory learning): notes are written after the reply and recalled
  with dates; she is the DeepSeek student, and `call_owner` names her owner as it does for every student; who
  someone is to her; dated plans published for speaking up; small talk never reaches the note writer; her
  impression is rewritten at most daily; the mood judged by her own model and told once; a speak-up is her impulse
  with no tools, moves her mood and writes no note, and the mark typed by a person is an ordinary message; her old
  memory settings are reported as moved.
* `VoiceTests` (the bot itself, with a fake channel): `/voice on` makes short answers voice messages too, once per
  answer and never code; her own first message is spoken; no second voice message when she sent one herself.
* `SpeakUpRuleTests`, `WhaleBotExtrasTests`: the speak-up rules, and only the owner steers them; a voice message is
  measured for Discord.

```
venv/bin/python -m unittest tests.test_plugin_whale_chan
```

`tests/test_core_boundaries.py` also checks that she imports only `contracts`, third-party packages
and her own folder.

## Building something similar

* **Contribute what works without the platform first**, and the interface only when it is configured
  ([`extension.py`](extension.py)). A module that every install gets but few use should do the same.
* **Declare settings with a check** ([`extension.py`](extension.py), [`extension.py`](extension.py)):
  `python -m core setup` and `$setup` then offer them, and a token is tried before it is saved.
* **Suggest defaults for your own addresses only**: `<interface>:*` assignments, admission and
  permissions ([`extension.py`](extension.py)). Anything else refuses to load.
* **Name the student, don't be one**: an interface for a character takes her persona's name (`persona`), so
  she is the same character, with the same memory, wherever she is reached.
* **Use the harness that fits, with options**: her chats are MomoChat's, set up as a chat client by the assignment's
  `options`, rather than an engine of her own. Write an engine only for a job none of them does
  ([docs/DESIGN.md](../../docs/DESIGN.md)).
* **An interface that only translates**: platform facts in `Request` ([`bot.py`](bot.py)),
  owners from the platform ([`bot.py`](bot.py)), readiness ([`bot.py`](bot.py)), the
  command catalog as native commands ([`bot.py`](bot.py), [`slash.py`](slash.py)) and
  transport state in the bottle.
* **Keep blocking work off the event loop**: math is rendered with `asyncio.to_thread`
  ([`bot.py`](bot.py)).

Pitfalls:

* `permissions:` in her `config.yml` may set `use` and `tiers` only for her interface's addresses. With
  no token there is no interface, so such a section makes the core refuse to start; after renaming
  `interface`, rename the `"whale_chan:*"` keys too.
* The progress line has no Cancel button; use `$cancel`.

See also: [plugins/README.md](../README.md), [Writing modules](../../docs/core/EXTENSIONS.md) (in
particular [Layout and declaration](../../docs/core/EXTENSIONS.md#layout-and-declaration),
[Interfaces](../../docs/core/EXTENSIONS.md#interfaces), [Engines](../../docs/core/EXTENSIONS.md#engines)
and [The built-in `momochat` engine](../../docs/core/EXTENSIONS.md#the-built-in-momochat-engine)),
[ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md), the built-in [`discord`](../../extensions/discord/README.md)
interface and [`momochat`](../../extensions/momochat/README.md) engine, and the [`webui`](../webui/README.md) plugin.
