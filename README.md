# DiscordStudyBot

A personal AI bot for Discord (and optionally WeChat) built on a small plug-in core. Models from
DeepSeek, OpenAI, Gemini and Claude talk, use tools, remember people and work together; everything
that touches a platform, decides what to say or does work is a module.

## Design in short

* **The core stays small.** It runs conversations, approvals, routines and history. It knows no platform.
* **Everything else is a module**: an interface (Discord), an engine (the assistant), or tools.
* **Don't touch the core if it can be a module.** If a module needs something new, add it to the
  contracts for every module, never for one platform.
* **Interfaces translate.** A platform's habits (slash commands, threads, reactions) become the core's
  plain facts in its interface, so the core never special-cases a platform.
* **`extensions/` is built-in**: maintained and tested here, trusted like the core.
  **`plugins/` is external**: someone else's code, or code for one person or machine.
* **Everything loads; you exclude.** Both kinds load by default; `modules: {exclude: [...]}` turns one off.
* **Your machine is yours.** Anything that touches the computer is creator-only, and a model's
  actions on it wait for your approval.

## Quick start

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an
   application, add a bot, copy its token and enable **Message Content Intent** (Bot → Privileged
   Gateway Intents).
2. `python3 -m venv venv && venv/bin/pip install -r requirements.txt` (install `ffmpeg` for videos and
   music, and `libopus0` for voice).
3. `cp .env.example .env` and set `DISCORD_BOT_TOKEN` plus at least one model key (`DEEPSEEK_API_KEY`,
   `OPENAI_API_KEY`, `GEMINI_API_KEY` or `CLAUDE_API_KEY`).
4. `venv/bin/python -m core`. It prints an invite link if the bot is in no server yet.
5. DM the bot, or mention it (or reply to it) in a server. The application's owner is the bot's owner:
   approvals (`$approve <id>`) arrive in their DM.

`core.yml` is optional. Copy [`core.example.yml`](core.example.yml) to change models, personas, which
channels the bot answers in and how, or to turn modules off. `venv/bin/python -m core --check` validates
the configuration and every module without starting anything.

## What it can do

Ask in plain language; the bot uses its tools when they help. Most tools are also commands: type
`/` in Discord for the menu (or `$name …` as text, which works everywhere and needs no @mention);
`/help` lists them. Approvals arrive with Approve and Deny buttons.

| | |
|---|---|
| Chat | The assistant engine (one persona), or the **room**: Luna, Gemini, Claude and DeepSeek sharing one work board, handing each other jobs, in parallel where independent |
| Memory | Personas keep short notes about each person per conversation; `$memory` shows, forgets or clears yours |
| Images | Attached and recent images are shown to the models; `$draw <prompt>` (Gemini or OpenAI) |
| Web | `$search`, `$read <url>`, image search, Google Maps, `$wolfram` |
| Video | `$video <url>` fitted to the upload limit; `$clip -u <url> -s 1:05 -e 1:20` then `$clip --confirm` |
| Reminders | `$remind 10m drink water`, `$reminders`, `$unremind <id>` |
| Routines | `$routine every 1h $status`, `$routine weekdays 09:00 <question for the bot>`, or just ask ("check the server every hour"); `$routine` lists, `$routine pause/resume/delete <id>` |
| Admins | The creator adds admins (`$admin add --user <id>`); admins see every chat's routines and runs, and `$admin lock` limits the bot to admins while debugging |
| Discord | `$music` in your voice channel, `$time`, `$start`/`$stop` live study clock, `$$math$$` rendered as images; progress with a Cancel button, reactions (✅/❌/🛑) that press buttons, and optional per-task threads (`thread_channels`) |
| Host | `$status`; creator only: `$screenshot`, `$getfile <path>`, `$savefile` (attach a file), `$clipboard`, `$copy <text>`, `$open <url or file>`, `$processes`, or just ask (each action waits for your approval) |
| Terminal | Creator only: `$t <line>` types into a shell on the bot's machine (Windows or Linux), a whole channel can be a terminal, or just ask the bot to do something on the computer (each command waits for your approval) |
| Owner | `$approve`/`$deny`, `$runs`, `$cancel`, `$tools`, `$call <capability> <json>`, routes and policy through `core.*` capabilities |
| WeChat | Optional: one WeChat account as a second interface; see [docs/WECHAT.md](docs/WECHAT.md) |

Writes (sending elsewhere, changing settings, saving notes) need the owner's approval unless an
assignment pre-approves them. Every run is durable: approvals and questions wait across restarts.

## Modules

A module is a folder with an `extension.py`. Built-in ones live in [`extensions/`](extensions), external
ones are dropped into [`plugins/`](plugins); all load at startup unless `core.yml` excludes them
(`modules: {exclude: [extensions/terminal]}`), each with its own git-ignored `config.yml`.
[`plugins/whale_chan`](plugins/whale_chan) is a complete example: 鲸酱, a second Discord bot that chats
like a personal AI web client on DeepSeek.

* [docs/core/ARCHITECTURE.md](docs/core/ARCHITECTURE.md): how the core is built (interfaces, gateway,
  manager, engines, capabilities, storage) and its rules.
* [docs/core/EXTENSIONS.md](docs/core/EXTENSIONS.md): writing modules, configuration, policy.
* [plugins/README.md](plugins/README.md): external modules, settings and secrets.

## Running as a service

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
extensions/   built-in modules (discord, cli, assistant, room, terminal, host, basic, media, images, web, reminders, system)
plugins/      external modules (whale_chan example, wechat)
tests/        tests and fixtures
docs/         architecture, module guide, WeChat
```
