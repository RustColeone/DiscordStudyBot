# `discord`: the built-in Discord interface

This module connects the bot to Discord. Direct messages and server channels (and threads) become
conversations; slash commands, message actions, buttons and reactions are translated into the core's
`$` commands; replies come back with Markdown, speaker labels, edited progress messages and math
rendered as images. It also brings a few Discord-only extras ported from the older bot: a music player
in the caller's voice channel, a study clock and a live clock message. It is built in and loads by
default. Study it when you want to connect a full chat platform to the core: it uses almost every part
of the interface contract (the port, the bottle, operations, commands, notices, progress, actions,
owners and readiness).

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `discord` | `DiscordInterface` in [`interface.py`](interface.py). Addresses are `discord:dm:<user id>` for direct messages and `discord:<channel id>` for server channels and threads. Display: Markdown, edits, progress, quotes, files, 2000 characters per message; the upload limit is the server's file size limit (10 MB in DMs). |
| Engines, personas, providers, adapters, profiles | none | Conversations use whatever engine the core assigns. |
| Default assignments, admission, permissions | none | The core's built-in defaults apply: group messages need a mention, and `core/defaults.py` (`BASIC_FEATURES`) sets `auto` rules for `discord.music`, `discord.music_control`, `discord.queue_music`, `discord.clock_start` and `discord.clock_stop`, so anyone can use them without asking. |

Operations (each becomes the capability `discord.<name>`; see
[Operations](../../docs/core/EXTENSIONS.md#operations)):

| Capability | Tier | Access | `$command` | Attributes | What it does |
| --- | --- | --- | --- | --- | --- |
| `discord.status` | read | creator | `$discord_status` | `private` | Login name, latency, servers (first 10) and application owners. |
| `discord.invite` | read | creator | `$invite` | | The OAuth link for inviting the bot to a server. |
| `discord.music` | write | member | `$music <text>` (argument `command`) | timeout 180 s | The music player, driven by the legacy text syntax (see [Using it](#using-it)). |
| `discord.music_status` | read | member | | | The current track and player state. |
| `discord.music_control` | write | member | | timeout 180 s | `action`: `initialize`, `play`, `pause`, `stop`, `next` or `previous`. |
| `discord.queue_music` | write | member | | timeout 180 s | `url` (required), `play_now` (boolean): add a YouTube URL after the current song, or play it now. |
| `discord.time` | read | member | `$time` | | The study clock as text. |
| `discord.clock_start` | write | member | `$start` | | Start a live clock message in this conversation. |
| `discord.clock_stop` | write | member | `$stop` | | Stop it. |

`music_status`, `music_control` and `queue_music` have no `$command`; they exist for models, which
call them with structured arguments instead of the `$music` text.

Declared settings (`EXTENSION.settings` in [`extension.py`](extension.py); settable with
`python -m core setup`, and all but the token with `$setup discord <key> <value>`):

| Key | Kind | Default | Env var |
| --- | --- | --- | --- |
| `token` | secret (asked on first run, checked by logging in) | | `DISCORD_BOT_TOKEN`, `DISCORD_TOKEN`, `TOKEN` |
| `thread_channels` | list | `[]` | |
| `talk_prefixes` | list | `[]` | |
| `slash_commands` | boolean | `true` | |
| `attachment_max_mb` | number | `25` | |
| `math` | boolean | `true` | |
| `music_dir` | path | `<data dir>/music` | |
| `music_list` | path | none | |

## Setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an
   application with a bot, copy its token (Bot → Reset Token) and turn on **Message Content Intent**
   (Bot → Privileged Gateway Intents).
2. Run `venv/bin/python -m core setup`. It asks for the token, saves it to `.env` as
   `DISCORD_BOT_TOKEN`, and checks it: it logs in over HTTP and reports the bot's name, whether the
   Message Content intent is on, and an invite link.
3. Open the invite link to add the bot to a server. `$invite` (owner only) prints it again later.

The owner is found automatically: at login the interface reports the application's owner (or every
member of the owning team) with `port.set_owners`, and offers the first owner's DM as the control
route. Both are used only when `core.yml` configures no owners or control route for Discord
([Owners and control](../../docs/core/ARCHITECTURE.md#owners-and-control)).

Settings live in `extensions/discord/config.yml` (git-ignored; copy
[`config.example.yml`](config.example.yml)); `extension_config.discord` in `core.yml` overrides it.

```yaml
token_env: [DISCORD_BOT_TOKEN, DISCORD_TOKEN, TOKEN]   # where the bot token is read from (.env)
attachment_max_mb: 25
slash_commands: true            # register the commands as Discord slash commands
talk_prefixes: []               # e.g. ["$agent", "$chat"]: "$agent hi" addresses the bot like a mention
thread_channels: []             # channel IDs where a task asked to the bot gets its own thread
music_dir: /path/to/music       # $music (needs ffmpeg and libopus)
music_list: /path/to/musicList.txt
ffmpeg: ffmpeg
clock_interval_s: 1             # live $start clock
clock_max: 10
math: true                      # render $$math$$ as images
math_max_images: 10
```

| Key | Meaning |
| --- | --- |
| `token_env` | Environment variable name(s) to read the token from, first non-empty wins. Use a different name to run a second bot from a copy of this module. |
| `attachment_max_mb` | Attachments larger than this are not downloaded (the message still arrives, labelled with its kind). |
| `slash_commands` | `false` stops registering the command catalog as Discord application commands. Typed `$` commands keep working. |
| `talk_prefixes` | A message that is exactly a prefix or starts with `prefix ` is addressed to the bot like a mention; the prefix is removed. |
| `thread_channels` | Channel IDs where a message addressed to the bot (not a command) opens a thread on that message and the run happens in the thread. |
| `music_dir` | Folder of audio files (`.mp3 .m4a .ogg .opus .wav .flac .webm .aac`) for `$music`. Defaults to `music/` in the module's data directory. The example value is a placeholder, not the default. |
| `music_list` | Optional playlist file in the legacy `musicList.txt` format: an optional first line with the 1-based current index, then one file name (from `music_dir`) or URL per line. When it exists it seeds the playlist instead of the folder listing. |
| `ffmpeg` | The ffmpeg executable used for playback. |
| `clock_interval_s` | Seconds between edits of a live clock (at least 1). |
| `clock_max` | Live clocks that may run at once across all conversations (at least 1). |
| `math` | `false` sends math as plain text. |
| `math_max_images` | Display equations rendered as images per reply; further ones are sent as text. |

Further math keys read by [`math_render.py`](math_render.py) but not in the example: `math_width_px`
(1440, at least 640), `math_dpi` (180, at least 72), `math_font_size_pt` (20, at least 12) and
`math_min_height_px` (220, at least 120).

**Environment variables:** `DISCORD_BOT_TOKEN` (or `DISCORD_TOKEN`, `TOKEN`, or the names in
`token_env`). Set the owner in `core.yml` (`owners: {discord: ["<user ID>"]}` and
`control: "discord:dm:<user ID>"`), not in `.env`: `.env` holds secrets only. The core still reads the
older `DISCORD_OWNER_ID` (or `CREATOR_USER_ID`) when `core.yml` sets no owner.

**Packages and tools** (all in `requirements.txt`):

| Need | For |
| --- | --- |
| `discord.py[voice]` (brings `aiohttp`) | Everything. |
| `PyNaCl`, `davey`, the system library `libopus` (`libopus0`), the `ffmpeg` program | Voice playback. Without them music commands fail with a message saying what is missing; nothing else is affected. |
| `yt-dlp` | YouTube URLs in the music player. |
| `matplotlib` | Display math as PNG images (MathText, no LaTeX install needed). |
| `pylatexenc` | LaTeX commands in inline math (`\alpha`, `\to`) converted to Unicode. Optional: without it they stay as typed; superscripts, subscripts, `\frac` and `\sqrt` are still converted. |

**Turning it off:** in `core.yml`,

```yaml
modules:
  exclude: [extensions/discord]
```

Without a token the module still loads, prints how to set one, and reports itself not ready
(`port.set_ready(False, ...)`), so nothing else breaks.

## Using it

**Talking to the bot.** In a DM every message is addressed to the bot. In a server channel a message
is addressed when it mentions the bot, replies to one of the bot's messages, starts with a
`talk_prefixes` word, is a typed command that exists in the catalog (`$help`, `$remind 10m tea`; an
unknown one such as `$AAPL is up` stays ordinary chatter), or is in a thread the bot opened or that
someone started on one of the bot's messages. Whether an addressed message starts a run is then up
to the core's admission rules.

**Slash commands and menus.** Type `/` for the command catalog. A slash command shows "thinking…",
and the first reply (or the run's progress) replaces it; later replies follow up. Private replies are
ephemeral. Commands that declare a `message_action` appear under Apps on a message's menu (up to five).
Creator-only commands are hidden from members in servers.

**Buttons and reactions.** Replies with actions (Approve, Deny, Cancel) carry buttons; a press runs the
button's `$command` as the person who pressed it. Reacting to such a message presses a button:
✅ or 👍 the first success/primary button, ❌, 🛑 or 👎 the first danger button.

**Progress.** A run shows one `⏳` message edited in place with a Cancel button; it disappears when the
answer arrives. Information nobody asked for (progress of a run in another conversation, failure
details, relay confirmations, interface events without an origin chat) is appended to a single
rolling code block per conversation, which accepts lines for up to an hour.

**Threads.** With `thread_channels: ["123456789012345678"]`, asking the bot something in that channel
opens a thread named after the message (archived after a day idle) and the run happens there. The
thread starts fresh: it has its own address and does not see the channel's history.

**Music** (in a server text channel, while you are in one of its voice channels; one player per server):

```text
$music init                 join your voice channel and play the playlist
$music play | pause | stop  (stop also leaves the voice channel)
$music next | prev
$music status               current track and state
$music queue                the playlist around the current track
$music -y <url> [url…]      play YouTube URLs now (also: $music play <url>, or a bare URL)
$music -y <url> --queue     add after the current song (also: $music queue <url>)
```

Aliases: `join`/`initialize`, `resume`, `leave`, `skip`, `previous`/`back`, `name`/`now`, `list`, and
the legacy flags (`--init`, `-p`, `--pause`, `-s`, `-n`, `--prev`, `--name`, `--list`, `-q`). The
playlist loops, keeps at most 200 tracks and is remembered per server across restarts. A track that
fails to load is skipped when advancing.

**Clock.** `$time` shows big UTC minutes:seconds ("Break Time" from minute 45) plus Beijing,
Los Angeles and London times. `$start` posts the same clock and edits it every `clock_interval_s`;
`$stop` ends it. One live clock per conversation, `clock_max` in total.

**Math.** In replies, inline `$…$` or `\(…\)` becomes Unicode (`$x^2$` → `x²`) when it looks like math
(`$5 and $10` stays as is); display `$$…$$` or `\[…\]` is sent as a PNG image between the text parts.
Code blocks are left alone. Owner relays are never converted.

**Owner commands.** `$discord_status` (only where only the owner reads) and `$invite`.

**Limits.** Long replies are split at paragraph, line or word boundaries into 2000-character
messages; only the first part quotes the request. At most five buttons per reply. An interaction can
be answered for about 15 minutes (the code stops at 14); after that, replies go to the channel as
ordinary messages, so a `private` reply is then visible to everyone in the channel.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION`, the declared `Setting`s, `check_token` (HTTP login used by setup), and `setup`, which reads the token from the environment (else a `token:` in its `config.yml`) and builds the interface. |
| [`interface.py`](interface.py) | `DiscordInterface`: lifecycle, Discord events to `Request`s, rendering and delivery of every `Reply` kind, progress, buttons and reactions, operations. |
| [`slash.py`](slash.py) | Pure translation: the core's `Command` catalog to application-command JSON (`payload`), an invocation back to option values (`find`, `values`), and `Interactions`, the open interactions by request ID. |
| [`status_log.py`](status_log.py) | `StatusLog`: the rolling code-block message for unrequested lines (one per conversation, up to an hour or 1900 characters). |
| [`music.py`](music.py) | `parse_music_command` (legacy syntax) and `MusicPlayer`: one looping playlist per server, voice connection, yt-dlp and ffmpeg sources. |
| [`clock.py`](clock.py) | The ASCII study clock and `LiveClocks`, one edited message per conversation. |
| [`math_render.py`](math_render.py) | `split_math` (text and display formulas in reading order, code untouched), inline math to Unicode, and `MathRenderer` (Matplotlib PNG on a fixed canvas). |
| [`config.example.yml`](config.example.yml) | Documented settings to copy into `config.yml`. |

**Startup.** `setup(context)` returns `Contributions(interfaces=[DiscordInterface(...)])`; it does no
network work. `start(port)` keeps the port, prunes old attachments, creates a `discord.Client` with the
Message Content intent and `AllowedMentions.none()`, and runs it as a task. A rejected token or a
missing intent becomes `port.set_ready(False, reason)` rather than an exception. `on_ready` reports
owners (`port.set_owners(owners, control=Address("discord", "dm:<first owner>"))`), calls
`port.set_ready(True, ...)` and registers slash commands.

**Incoming messages** (`on_message`). Bot authors are ignored. `to_request` builds the
[`Request`](../../docs/core/EXTENSIONS.md#submitting-requests): the message ID as `id`, the bot mention
and any talk prefix stripped, `mentions_me` from the facts listed above (a command counts when
`names_command` finds it in `port.commands()`), `reply_to` and `quoted_text` from the replied message,
`kind` from the first attachment's content type, attachments downloaded only when addressed, and
`extra` with `guild_id` and `channel_name`. In a thread channel, `_open_thread` creates the thread and
re-addresses the request to it with `dataclasses.replace`. Submission happens under a per-conversation
lock so one conversation's messages reach `port.submit` in order. An `accepted` result shows the typing
indicator.

**Slash commands, buttons, reactions.** `sync_commands` sends `slash.payload(port.commands())` with
one bulk overwrite, only when its SHA-256 fingerprint differs from the one in the bottle. An invocation
is deferred at once, its options turned into `$` text with `Command.render(values)`, and submitted as
the invoking user (`mentions_me=True`, `extra["via"] = "interaction"`). The interaction is kept in
`Interactions` under the request ID, so `deliver` can answer it: `_poster` edits the placeholder for the
first reply, deletes it and sends an ephemeral follow-up for a private reply, and uses follow-ups after
that. If admission refuses a slash command (`disabled`, `sender_not_allowed`, `owner_only`), the
placeholder says why. Buttons carry `cmd:<command>` as their custom ID, so a press after a restart still
works; the press is submitted as the presser. For reactions, the actions of each message with buttons
are kept in the bottle (`message_actions`) and `on_raw_reaction_add` submits the matching command.

**Delivery** (`deliver(reply)`). Replies that `is_status` marks as unrequested go to the `StatusLog`.
`progress` goes to `_show_progress`: one message per `run_id`, edited in place, inside the slash
command's placeholder when there is one; `_editable` switches to editing through the channel once the
interaction has expired. Everything else is rendered by `render`: notices by code (`approval_request`
with tier, capability, arguments and the `$approve`/`$deny` lines; `reminder` with a ping for the person
who set it and a "due" line when late; `relayed`; the short `NOTICES` texts), events with a `📨` and
their origin, text with a `**speaker · label**` line and a "from" line for routed replies. Text is
split and, when math is on, interleaved with formula images (`_text_items`; rendering runs in a worker
thread). The `Delivery` follows the contract: `Forbidden`/`NotFound` before anything was sent is
certain, other HTTP 5xx, timeouts and partial sends are `uncertain`. Afterwards the run's progress
message is removed, unless the answer took over its placeholder.

**Operations** (`operate(operation, arguments, context)`). The public ones act only on
`context.address`: the clock in that conversation, music in that conversation's server and in the
voice channel of `context.sender`. Music refuses DMs. Results are `ToolResult.ok` / `ToolResult.fail`
with error codes such as `not_in_voice`, `voice_unavailable`, `not_initialized`, `missing_dependency`.

**Design decisions.**

* A thread's ID is its starter message's ID. `is_bot_thread` uses that to tell whether a thread was
  started on a bot message, checking the bot's recent sent IDs first and fetching the starter only
  once per thread.
* Slash registration is fingerprinted because Discord rate-limits command registration and a
  restart should not re-register an unchanged catalog.
* Unrequested information shares one edited message per conversation so the owner's DM is not
  flooded by progress and failure details from other chats.
* Music state lives in the bottle, so the playlist and position survive restarts. Each play bumps a
  `generation` counter; the "track finished" callback, which discord.py calls from its player thread,
  is moved to the event loop with `call_soon_threadsafe` and ignored if the generation changed.
* `MusicPlayer.voice_problem`, `extract` and `make_source` are small methods so tests can replace the
  platform parts.

**Safety.**

* Access and approval are decided by the core, not here. The interface marks creator-only commands
  with `default_member_permissions: "0"` to hide them from members, but that is cosmetic; the core
  enforces the level. `discord.status` is `private`, so it is offered only in conversations private to
  the owner. `discord.invite` is owner-only.
* Music and clock writes are `write` tier. They run without asking only because of the core's default
  `auto` rules; remove those rules and they wait for approval like other writes.
* Buttons and reactions are submitted as the person who pressed or reacted, so pressing Approve does
  nothing for someone who may not approve.
* The client sends with `AllowedMentions.none()`; only a reminder pings, and only the user who set it.
  Approval arguments are shown inside code spans or a JSON block with backticks neutralised.
* Attachments are downloaded only for addressed messages, up to `attachment_max_mb`, with sanitised
  file names, into the module's data directory.
* The token is read from the environment and never shown; `$setup` cannot set it (secrets are set on
  the machine with `python -m core setup`). Log messages carry error types, not message text.

## Data it keeps

Under the module's data directory (`data/core/extensions/discord/` with the default `data_dir`):

| Path | What | Retention |
| --- | --- | --- |
| `attachments/<message or interaction id>/` | Downloaded attachments, passed to the core as `FileRef`s. | Folders older than 7 days are deleted when the interface starts. |
| `math/formula-*.png` | Rendered equations. | Deleted right after the reply is sent. |
| `music/` | The default `music_dir`. The module only reads it. | Yours. |

In the interface's bottle (`port.bottle`): `slash_fingerprint`, `message_actions` (button actions of
the last 300 messages with buttons, for reactions) and `music:<guild id>` (playlist of up to 200 tracks
and the current index). Everything else (channel cache, progress messages, open interactions, status
blocks, live clocks, resolved YouTube stream URLs for 10 minutes) is in memory and bounded.

## Tests

| File | Covers |
| --- | --- |
| [`tests/test_core_discord.py`](../../tests/test_core_discord.py) | The interface inside a real core with a fake discord.py client: DMs, mentions and replies in servers, talk prefixes, known commands addressing the bot, threads (`thread_channels` and threads on bot messages), splitting and quoting, application owner and control route, progress and Cancel by reaction, attachments only when addressed, certain and uncertain delivery failures, event relays, the rolling status block, approval and reminder rendering, speaker labels, a missing token. Also token lookup order and the default module loading. |
| [`tests/test_core_discord_slash.py`](../../tests/test_core_discord_slash.py) | `slash.payload` and `slash.values`; registration only when the catalog changes; answering, ephemeral and progress-in-placeholder behaviour; progress after the interaction expires; buttons run as the presser; refusal messages. |
| [`tests/test_core_discord_extras.py`](../../tests/test_core_discord_extras.py) | Music (declared tiers and access, playlist and voice channel, model operations, missing voice support, stop on shutdown), the clock (text, live edits, approval without the `auto` rules) and math (inline Unicode, images between text parts, fixed canvas, fallbacks, `math: false`). |

```bash
venv/bin/python -m unittest tests.test_core_discord tests.test_core_discord_slash tests.test_core_discord_extras
```

## Building something similar

* **Never fail at load for a missing secret.** [`extension.py:40`](extension.py) builds the interface
  with an empty token; [`interface.py:171`](interface.py) reports not ready and returns. A `Setting`
  with `check=` ([`extension.py:14`](extension.py)) gives setup a cheap login test.
* **Report facts, let the core decide.** [`interface.py:354`](interface.py) (`to_request`) computes
  `mentions_me` from platform metadata and leaves admission to the core; it never replies on its own.
* **Map the platform's owner** with `port.set_owners` ([`interface.py:223`](interface.py)) so a fresh
  install routes approvals somewhere private without configuration.
* **Translate native menus into `$` text** ([`slash.py:29`](slash.py), [`interface.py:443`](interface.py)):
  the core only ever sees `Command.render(values)`, so typed and menu commands behave the same.
* **Keep cheap durable state in the bottle** and prune it yourself (`message_actions` at
  [`interface.py:305`](interface.py), `ACTION_MEMORY = 300`; music at [`music.py:177`](music.py)).
* **Classify delivery failures honestly** ([`interface.py:687`](interface.py)): report `uncertain`
  whenever part of a reply may have been sent, and return the first message ID.
* **Progress as one edited message** keyed by `run_id`, removed when the answer arrives
  ([`interface.py:732`](interface.py), [`interface.py:774`](interface.py)).
* **Public operations act only on the caller's conversation** ([`interface.py:794`](interface.py),
  [`interface.py:827`](interface.py)), and blocking work goes to a thread (`asyncio.to_thread` for
  yt-dlp and Matplotlib).

Pitfalls:

* Keep every in-memory cache bounded (this module caps each `OrderedDict`); a bot runs for months.
* Submit one conversation's messages in order (the per-conversation lock around `port.submit`);
  `deliver` does not take that lock, since the core may call it concurrently.
* Discord's 3-second interaction deadline: defer first, then do the work.
* A button's custom ID is limited to 100 characters, and the command is cut to fit; keep button
  commands short.
* Do not import from other modules or the core; copy what you need. `plugins/whale_chan` carries its
  own copies of `slash.py` and `math_render.py` for that reason.

See also: [Writing modules](../../docs/core/EXTENSIONS.md) (especially
[Interfaces](../../docs/core/EXTENSIONS.md#interfaces) and
[The included Discord interface](../../docs/core/EXTENSIONS.md#the-included-discord-interface)),
[Core architecture](../../docs/core/ARCHITECTURE.md) ([Permissions](../../docs/core/ARCHITECTURE.md#permissions)),
[`extensions/cli`](../cli) (the smallest interface), [`plugins/webui`](../../plugins/webui) (a web
interface), [`plugins/whale_chan`](../../plugins/whale_chan) (a second Discord bot as an external module).
