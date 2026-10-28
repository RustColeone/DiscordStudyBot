# DiscordStudyBot

A personal AI bot for Discord (and optionally WeChat) built on a small plug-in core. Models from
DeepSeek, OpenAI, Gemini and Claude talk, use tools, remember people and work together; everything
that touches a platform, decides what to say or does work is a module.

## Design in short

* **The core stays small.** It runs conversations, approvals, routines and history. It knows no platform.
* **Everything else is a module**: an interface (Discord), an engine (MomoChat, Chimera), the students, or tools.
* **Don't touch the core if it can be a module.** If a module needs something new, add it to the
  contracts for every module, never for one platform.
* **Interfaces translate.** A platform's habits (slash commands, threads, reactions) become the core's
  plain facts in its interface, so the core never special-cases a platform.
* **`extensions/` is built-in**: maintained and tested here, trusted like the core.
  **`plugins/` is external**: someone else's code, or code for one person or machine.
* **Everything loads; you exclude.** Both kinds load by default; `modules: {exclude: [...]}` turns one off.
* **Your machine is yours.** Anything that touches the computer is creator-only; a model's critical
  actions on it (shell lines, saving files, sending as you) wait for your approval, and secrets never
  go to models.

## Quick start

1. `python3 -m venv venv && venv/bin/pip install -r requirements.txt` (install `ffmpeg` for videos and
   music, and `libopus0` for voice).
2. `venv/bin/python -m core setup`. It asks for a model key (DeepSeek, OpenAI, Gemini or Claude) and tests
   it, lets you switch modules on or off, and asks what the modules need: for Discord, the token of a bot
   from the [Developer Portal](https://discord.com/developers/applications) (Bot → Reset Token, and turn
   on **Message Content Intent**), which it logs in with to check and gives you the invite link. Secrets
   are typed hidden and kept only in `.env`.
3. `venv/bin/python -m core`. At that console you are the bot's creator and can talk to it right away,
   even with nothing else connected.
4. DM the bot on Discord, or mention it (or reply to it) in a server. The Discord application's owner is
   the bot's creator there; approvals arrive in their DM with Approve and Deny buttons.
   No Discord? Open `http://<this computer>:4156` from any device at home: the web UI, where the five
   classmates answer in their own bubbles (see [plugins/README.md](plugins/README.md#the-web-ui)).

Later changes: run the setup again, or type `$setup` to the bot as its creator (modules and their
settings; never secrets). Shared settings (owners, models, personas, which channels it answers in) live
in `core.yml` (see [`core.example.yml`](core.example.yml)); `venv/bin/python -m core --check` validates
everything without starting.

## What it can do

Ask in plain language; the bot uses its tools when they help. Most tools are also commands: type
`/` in Discord for the menu (or `$name …` as text, which works everywhere and needs no @mention);
`/help` lists them. Approvals arrive with Approve and Deny buttons.

| | |
|---|---|
| Chat | **MomoChat**: one student or agent in a direct chat, or a group chat with GPT, Gemini, Claude, DeepSeek and Grok (those you call on answer, everyone for "you all", whose turn it is otherwise), handing each other parts of the work ("draw this", "search this") |
| Students and agents | The five students are agents with their memory: each keeps what she knows of you, her mood and her bond with you (mneme-llm), wherever she speaks; `$alias` names one your way, `$callme` says what they call you, `$link` joins another account of yours, `$bond` shows where each stands. An agent of your own is a prompt and its models in core.yml (`agents:`), with no memory |
| Stamina | Every answer is priced (Artificial Analysis's list); each student has a daily allowance and everyone together a daily total, and a run that reaches one stops and asks before spending more. Accounts show a balance the provider reports (DeepSeek, OpenRouter) or a budget you enter. `$stamina` shows and changes it all; so do the pages' header and the Students page |
| Pages | The web UI's Cafe, MomoChat and Students pages, your page (`/me`) and the Classroom share one header (the places, the day's stamina, your tab) and speak English or Chinese (picked on your page, else the browser's; the translations in `i18n-zh.js`, made by `tools/translate_ui.py` with DeepSeek). Your page holds what the students call you, your linked accounts, the theme, the language, replies read aloud and the day's total. The Students page shows each student's level, bond, art stages, bond pictures and her models; the art is drawn by `tools/draw_students.py` from [extensions/students/art.yml](extensions/students/art.yml) |
| Images | Attached and recent images are shown to the models; `$draw <prompt>` (Gemini or OpenAI) |
| Web | `$search`, `$read <url>`, image search, Google Maps, `$wolfram` |
| Video | `$video <url>` fitted to the upload limit; `$clip -u <url> -s 1:05 -e 1:20` then `$clip --confirm` |
| Reminders | `$remind 10m drink water`, `$reminders`, `$unremind <id>` |
| Routines | `$routine every 1h $status`, `$routine weekdays 09:00 <question for the bot>`, or just ask ("check the server every hour"); `$routine` lists, `$routine pause/resume/delete <id>` |
| Admins | The creator adds admins (`$admin add --user <id>`); admins see every chat's routines and runs, and `$admin lock` limits the bot to admins while debugging; the creator can `$admin restart` it |
| Discord | `$music` in your voice channel, `$time`, `$start`/`$stop` live study clock, `$$math$$` rendered as images; progress with a Cancel button, reactions (✅/❌/🛑) that press buttons, and optional per-task threads (`thread_channels`) |
| Host | `$status`; creator only: `$screenshot`, `$getfile <path>`, `$savefile` (attach a file), `$clipboard`, `$copy <text>`, `$open <url or file>`, `$processes`, or just ask (looking works in your private chats without asking; saving, opening and copying wait for your approval) |
| Terminal | Creator only: `$t <line>` types into a shell on the bot's machine (Windows or Linux), a whole channel can be a terminal, or just ask the bot to do something on the computer (each command waits for your approval) |
| Owner | `$approve <id> [always]`/`$deny`, `$permissions` (what runs here without asking, saved grants), `$runs`, `$cancel`, `$tools`, `$call <capability> <json>`, routes and policy through `core.*` capabilities |
| Web UI | The web UI on your local network (`:4156`): the Cafe, MomoChat, the Students page and your page, no Discord needed: the five students in their own bubbles with their own pictures, `@` to ask one of them, a class list, Markdown and LaTeX, files both ways, approval cards, live typing, a `$` command palette, chats you can pin, reorder and delete; optional passphrase |
| WeChat | Optional: one WeChat account as a second interface; see [docs/WECHAT.md](docs/WECHAT.md) |
| Chimera | Creator only: the students work on your code together, HydraFusion-style. Each task is routed to one model, a cheap draft with a quality gate, or a draft reviewed by another model family; it is worked in its own git worktree and tested, and lands as one commit only after your second check and final approval. Before anything comes to you, a tester tries to break it against the request's acceptance criteria (the old code against the new). The Classroom page at `:4157`, `club` in any project folder, or remotely from a Discord channel. `venv/bin/python -m extensions.chimera` runs the core with just the Classroom (see [extensions/chimera](extensions/chimera/README.md)) |

Reads and prepares run; writes and critical actions (sending elsewhere, changing settings, the shell)
ask the owner, except what `permissions:` in core.yml lets run (by default anyone may use reminders,
music, the study clock and their own routines). Approvals come to the chat you asked from when it is
your own direct chat with the bot, otherwise privately to the control route. Every run is durable:
approvals and questions wait across restarts. See [docs/core/ARCHITECTURE.md](docs/core/ARCHITECTURE.md#permissions).

## Modules

A module is a folder with an `extension.py`. Built-in ones live in [`extensions/`](extensions), external
ones are dropped into [`plugins/`](plugins); all load at startup unless `core.yml` excludes them
(`modules: {exclude: [extensions/terminal]}`), each with its own git-ignored `config.yml`.
[`plugins/whale_chan`](plugins/whale_chan) is a complete example: 鲸酱, a second Discord bot that chats
like a personal AI web client, as the DeepSeek student. [`plugins/webui`](plugins/webui) is the example interface: the
bot as a web page on your local network.

* [docs/DESIGN.md](docs/DESIGN.md): the decisions to read first: students are the models, engines are the
  harnesses (MomoChat converses, Chimera works), interfaces are the frontends; declared dependencies; the
  minimum core.
* [docs/core/ARCHITECTURE.md](docs/core/ARCHITECTURE.md): how the core is built (interfaces, gateway,
  manager, engines, capabilities, storage) and its rules.
* [docs/core/EXTENSIONS.md](docs/core/EXTENSIONS.md): writing modules, configuration, policy, and which
  module to read for what.
* [The architecture page](https://rustcoleone.github.io/DiscordStudyBot/architecture/): every module in the slots
  it fills, every file, class and function, read from the code (served from master's `docs/`; rebuild
  [`docs/architecture/data.js`](docs/architecture) with `venv/bin/python tools/architecture.py`).
* Each module's own `README.md` (for example [extensions/momochat](extensions/momochat/README.md)): what it adds,
  setup, how it is built, its tests and what to copy from it.
* [plugins/README.md](plugins/README.md): external modules, settings and secrets.

## Staying up

`python -m core` runs the bot under a small supervisor: the bot is its child process, sharing the same
console. When the bot crashes, stops responding for a minute, or the creator types `$admin restart`
(or presses **Restart now** after a `$setup` change), the supervisor starts a fresh one that re-reads
every setting and module, and the bot then tells you (in your DM, or the console) why and for how long
it was down. After five crashes within ten minutes it stops trying and says why.
`python -m core serve` runs the bot alone, without the supervisor.

To start it at boot, have the system run `python -m core`. On Linux, a systemd service (it only
starts the supervisor; restarts are the supervisor's job):

```ini
# /etc/systemd/system/discordstudybot.service
[Unit]
Description=Discord Study Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<you>
WorkingDirectory=/path/to/DiscordStudyBot
EnvironmentFile=/path/to/DiscordStudyBot/.env
ExecStart=/path/to/DiscordStudyBot/venv/bin/python -m core --config core.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

On Windows, a task that starts it when you log in (the host module needs your desktop session):
`schtasks /Create /SC ONLOGON /TN DiscordStudyBot /TR "C:\path\to\DiscordStudyBot\venv\Scripts\python.exe -m core --config C:\path\to\DiscordStudyBot\core.yml"`.

Runtime data (databases, module data, the audit log) lives in `data/core/`.

## Development

```bash
venv/bin/python -m unittest discover -s tests -t .
```

`contracts/` is the only code shared between the core and modules; `core/` never imports a
module and modules never import the core ([`tests/test_core_boundaries.py`](tests/test_core_boundaries.py)).

```
contracts/    public API between the core and modules
core/         gateway, manager, providers, storage, loader
extensions/   built-in modules (discord, cli, momochat, chimera, students, terminal, host, basic, media, images, web, reminders, system)
plugins/      external modules (whale_chan example, wechat)
tests/        tests and fixtures
docs/         architecture, module guide, WeChat
```
