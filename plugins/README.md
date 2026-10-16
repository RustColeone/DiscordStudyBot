# Drop-in extensions

Every folder in `plugins/` that contains an `extension.py` is loaded automatically at startup; nothing
needs to change in `core.yml`. To add one, drag its folder in and restart (`sudo systemctl restart
discordstudybot.service`). To turn one off, move it out (or set `plugin_dirs: []` in core.yml).

Each extension folder may keep its own `config.yml` next to `extension.py`: it becomes that
extension's settings, and `extension_config.<name>` in core.yml overrides it. `config.yml` files are
git-ignored; secrets still belong in `.env`. Folders in `plugins/` are git-ignored too (they are
usually separate repositories), except this README and the `scholar` example.

An extension is a black box to the core and the core to it: it imports only `contracts`, third-party
packages and its own folder. Besides interfaces, engines, capabilities and personas it may suggest
defaults so it works as soon as it is dropped in: model `profiles`, and `assignments` / `admission`
for its own interfaces. See [docs/core/EXTENSIONS.md](../docs/core/EXTENSIONS.md).

## The example: `scholar`

A second Discord bot with the `deliberate` engine, which plans, works with tools while showing a
progress line, and reviews its draft before answering. To try it:

1. Create a Discord application with a bot, enable **Message Content Intent**, invite it to a server.
2. Put `SCHOLAR_DISCORD_TOKEN=...` in `.env`.
3. `cp plugins/scholar/config.example.yml plugins/scholar/config.yml` and adjust models or effort.
4. Restart the bot. DM it, or mention it in a server.

Without a token only the engine and persona load; assign them anywhere in core.yml, for example
`"discord:123": {engine: deliberate, personas: [scholar]}`.
