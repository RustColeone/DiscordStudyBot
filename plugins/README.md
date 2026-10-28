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
example `token_env: DISCORD_BOT_TOKEN_WHALE_CHAN`). Only secrets: every value in `.env` is blanked out of
anything the bot shows, so ordinary settings (paths, URLs, IDs) go in the `config.yml`. If you would
rather not edit the environment, an extension may accept the secret in its `config.yml` instead
(`token: …`). In that case:

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

Her own Discord bot that chats like a personal AI web client (ChatGPT, Claude): a frontend of MomoChat, the
conversation harness. She is the DeepSeek student, the whale girl of the club, and the bot is one more way to reach
her: the same character, models and memory as in MomoChat's web page or the main bot.

* each DM or channel is one ongoing thread; send `/new` to start a fresh one;
* full Markdown answers, math rendered as images, image attachments understood;
* the core's tools when useful (web search, reading pages, drawing, videos, reminders, …), with a
  progress line while she works; only the answer is posted;
* she asks before every write or critical action (`always-ask`, no exceptions), in case she misread a
  request or a page tried to trick her; when you asked in your DM with her, the question comes from her,
  in that DM;
* her memory of each person and her mood are hers as a student ([the students module](../extensions/students/README.md)):
  one [mneme-llm](../../mneme-llm/README.md) file for DeepSeek, wherever you talk with her, so switching her model
  or the way you reach her keeps what she knows;
* speaking up first now and then (off until you turn it on with `/speakup`; it starts as a dry run);
* her voice: `/voice on` turns her short replies into Discord voice messages (OpenAI speech; Piper, a
  local voice, is optional);
* slash commands: `/new`, `/voice`, `/speakup` plus a short list from the core (`slash_commands` in her
  config: help, memory, remind, reminders, routine, draw, search, read, video by default).

To run it:

1. Create a Discord application with a bot, enable **Message Content Intent**, invite it to a server.
   For her memory and mood: `venv/bin/pip install -e ../mneme-llm`.
2. Put `DISCORD_BOT_TOKEN_WHALE_CHAN=...` in `.env` (or `token:` in her `config.yml`, see above).
3. Optional: `cp plugins/whale_chan/config.example.yml plugins/whale_chan/config.yml` to change effort, speaking up
   or her voice (her models and personality are in `extensions/students/deepseek/`).
4. Restart the bot. DM her, or mention her in a server.

Without a token only her voice loads. To have the main bot talk with her the same way somewhere, assign MomoChat
with her chats' options in core.yml, for example
`"discord:123": {engine: momochat, personas: [deepseek], options: {style: full, effort: high}}`.

## The web UI

[`webui`](webui) is MomoChat's page on your local network, for when you would rather not set up
Discord, and the example of an interface plugin. Open `http://<this computer>:4156` (the address is
printed at startup) from a laptop or phone at home:

* the page is laid out as a messenger: your chats on the left (her picture, your name for her, ♥ your bond
  and the last line said; search them), the chat in the middle (its header shows her mood and your bond), and on
  the right her card in a direct chat (her portrait, your bond, which model answers) or the class in a group chat.
  On a phone the places bar sits along the bottom and the chats and the card slide in;
* each chat is a MomoChat group chat by default: GPT, Gemini, Claude, DeepSeek and Grok,
  each in their own bubble with their own picture (the students module's; drop `gpt.png` and so on into
  [`webui/avatars/`](webui/avatars/README.md) to use others);
* the class list on the right (in a group chat) shows who is typing or speaking; click someone, or type `@`, to
  ask them directly (`@GPT …` gets GPT's answer alone); each card also shows your name for her, her bond with you
  (♥ and her closeness) and her mood, from her memory; when your bond with her moves, the chat says so;
* a student whose own company's key is not set still answers, on a classmate's model (her fallback): the chat
  list and her card say so;
* under each classmate, the model she answers with in this chat; click it (again to close) to choose
  another of the models the providers offer, or her own again (this is `$model`, for admins, asked in the
  background and kept per chat); each of her messages shows the model that wrote it;
* work notes fold away; Markdown, code and LaTeX are rendered in the page (KaTeX); files and pictures go
  both ways (drop, paste or 📎);
* approvals arrive as cards with their tier and buttons; type `$` (or `/`) for the command palette;
* chats on the left, each a separate conversation (`webui:<chat>`): drag to reorder, or ⋯ to rename,
  pin, move or delete (deleting removes its messages and files from the page; the core keeps its history);
* New chat asks who is in it: everyone, a few classmates, or one for a direct message (also 💬 on her
  card); direct messages have their own heading in the list;
* a message naming no one gets an answer from whoever's turn it is, and the others may chime in; a classmate
  may hand part of the work to another ("draw this", "search this"), who answers after her; long chats are
  summarised as they scroll out of view, and the classmates remember you across every web chat;
* without MomoChat (or the students) the page still opens, greyed, and says why.

Whoever opens the page is the bot's creator, like the console. So it answers only the local network
(private and link-local addresses, or `networks:`), refuses other websites in your browser and
DNS-rebinding hostnames, and serves only pictures, media and PDFs inline. Set `WEBUI_PASSPHRASE` in `.env`
(or `passphrase:` in its `config.yml`) to have each browser sign in once. Settings: see
[`webui/config.example.yml`](webui/config.example.yml) (port, your name, title, who answers).

