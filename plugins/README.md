# Drop-in extensions

Every folder in `plugins/` that contains an `extension.py` is loaded automatically at startup; nothing
needs to change in `core.yml`. To add one, drag its folder in and restart (`sudo systemctl restart
discordstudybot.service`). To turn one off, move it out (or set `plugin_dirs: []` in core.yml).

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

Folders in `plugins/` are git-ignored, since they are usually separate repositories; only this README
and the `whale_chan` example are tracked. Every `plugins/*/config.yml` and `extensions/*/config.yml`
is ignored too.

An extension is a black box to the core and the core to it: it imports only `contracts`, third-party
packages and its own folder. Besides interfaces, engines, capabilities and personas it may suggest
defaults so it works as soon as it is dropped in: model `profiles`, and `assignments` / `admission`
for its own interfaces. See [docs/core/EXTENSIONS.md](../docs/core/EXTENSIONS.md).

## The example: `whale_chan` (鲸酱)

Her own Discord bot that chats like a personal AI web client (ChatGPT, Claude), with the DeepSeek
whale-girl personality, on DeepSeek V4 Pro (DeepSeek Flash as fallback):

* each DM or channel is one ongoing thread; send `/new` to start a fresh one;
* full Markdown answers, math rendered as images, image attachments understood;
* the core's tools when useful (web search, reading pages, drawing, videos, reminders, …), with a
  progress line while she works; only the answer is posted;
* her own diary about each person (`$memory`).

To run it:

1. Create a Discord application with a bot, enable **Message Content Intent**, invite it to a server.
2. Put `DISCORD_BOT_TOKEN_WHALE_CHAN=...` in `.env` (or `token:` in her `config.yml`, see above).
3. Optional: `cp plugins/whale_chan/config.example.yml plugins/whale_chan/config.yml` to change models,
   effort or the personality.
4. Restart the bot. DM her, or mention her in a server.

Without a token only the `chat` engine and `whale_chan` persona load; assign them anywhere in
core.yml, for example `"discord:123": {engine: chat, personas: [whale_chan]}`.
