# External modules (plugins)

A module is a folder with an `extension.py`. The built-in ones live in `extensions/`; this folder holds
external ones: someone else's code, or code specific to one person or machine. Every module, built-in or
external, loads at startup unless core.yml excludes it. To add one, drag its folder in and restart
(`sudo systemctl restart discordstudybot.service`). To turn one off, move it out or exclude it:
`modules: {exclude: [plugins/name]}` in core.yml.

## Settings and secrets

Each extension folder may keep its own `config.yml` next to `extension.py`: it becomes that
extension's settings, and `extension_config.<name>` in core.yml overrides it. Usually the folder ships
a documented `config.example.yml` to copy.

Secrets (bot tokens, API keys) are best kept in `.env`, with the config naming the variable (for
example `token_env: DISCORD_BOT_TOKEN_WHALE_CHAN`). If you would rather not edit the environment, an
extension may accept the secret in its `config.yml` instead (`token: …`). In that case:

* `chmod 600 config.yml`, so only the bot's user can read it;
* never share, zip or copy the folder together with its `config.yml`;
* if the folder is its own git repository, keep a `.gitignore` in it listing `config.yml` (this
  repository's own `.gitignore` does not apply inside another repository).

## What git ignores

Folders in `plugins/` are git-ignored, since they are usually separate repositories; only this README,
the `whale_chan` and `webui` examples and `wechat` are tracked. Every `plugins/*/config.yml` and `extensions/*/config.yml`
is ignored too.

An extension is a black box to the core and the core to it: it imports only `contracts`, third-party
packages and its own folder. Besides interfaces, engines, capabilities and personas it may suggest
defaults so it works as soon as it is dropped in: model `profiles`, and `assignments` / `admission`
for its own interfaces. See [docs/core/EXTENSIONS.md](../docs/core/EXTENSIONS.md).

## The example: `whale_chan` (鲸酱)

Her own Discord bot that chats like a personal AI web client (ChatGPT, Claude), with the DeepSeek
whale-girl personality, on DeepSeek Flash:

* each DM or channel is one ongoing thread; send `/new` to start a fresh one;
* full Markdown answers, math rendered as images, image attachments understood;
* the core's tools when useful (web search, reading pages, drawing, videos, reminders, …), with a
  progress line while she works; only the answer is posted;
* she asks before every write or critical action (`always-ask`, no exceptions), in case she misread a
  request or a page tried to trick her; when you asked in your DM with her, the question comes from her,
  in that DM;
* her own diary about each person (`$memory`);
* slash commands: `/new` plus a short list from the core (`slash_commands` in her config:
  help, memory, remind, reminders, routine, draw, search, read, video by default).

To run it:

1. Create a Discord application with a bot, enable **Message Content Intent**, invite it to a server.
2. Put `DISCORD_BOT_TOKEN_WHALE_CHAN=...` in `.env` (or `token:` in her `config.yml`, see above).
3. Optional: `cp plugins/whale_chan/config.example.yml plugins/whale_chan/config.yml` to change models,
   effort or the personality.
4. Restart the bot. DM her, or mention her in a server.

Without a token only the `chat` engine and `whale_chan` persona load; assign them anywhere in
core.yml, for example `"discord:123": {engine: chat, personas: [whale_chan]}`.

## The web UI

[`webui`](webui) is the bot as a chat page on your local network, for when you would rather not set up
Discord, and the example of an interface plugin. Open `http://<this computer>:4156` (the address is
printed at startup) from a laptop or phone at home:

* each chat is a conversation of the shared room by default: Luna, Gemini, Claude, DeepSeek and Grok,
  each in their own bubble with their own picture (drop `luna.png` and so on into
  [`webui/avatars/`](webui/avatars/README.md); until then, coloured initials);
* the class list on the right (👥; open on a desktop, closed on a phone) shows who is typing or speaking;
  click someone, or type `@`, to ask them directly (`@Luna …` gets Luna's answer alone);
* work notes fold away; Markdown, code and LaTeX are rendered in the page (KaTeX); files and pictures go
  both ways (drop, paste or 📎);
* approvals arrive as cards with their tier and buttons; type `$` (or `/`) for the command palette;
* chats in the sidebar, each a separate conversation (`webui:<chat>`): drag to reorder, or ⋯ to rename,
  pin, move or delete (deleting removes its messages and files from the page; the core keeps its history).

Whoever opens the page is the bot's creator, like the console. So it answers only the local network
(private and link-local addresses, or `networks:`), refuses other websites in your browser and
DNS-rebinding hostnames, and serves only pictures and media inline. Set `WEBUI_PASSPHRASE` in `.env`
(or `passphrase:` in its `config.yml`) to have each browser sign in once. Settings: see
[`webui/config.example.yml`](webui/config.example.yml) (port, your name, title, who answers).

