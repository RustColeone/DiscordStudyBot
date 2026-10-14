# Bot debugging

The bot is managed by `discordstudybot.service`. Its deployment template is
`../server-mgmt/discordstudybot.service`, and the installed unit is
`/etc/systemd/system/discordstudybot.service`. The working directory is this repository;
the launch command is `venv/bin/python main.py --mode discord`. Runtime environment
settings come from `.env`. Avoid dumping environment files or API keys while collecting logs.

These commands only read the bot's state:

```bash
systemctl show discordstudybot.service \
  --property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,WorkingDirectory
journalctl -u discordstudybot.service --since '10 minutes ago' --no-pager -o short-iso
journalctl -u discordstudybot.service --since '1 hour ago' --no-pager -o short-iso \
  --grep 'chat timing|agent timing|consultation failed|Queued command failed'
tail -100 chat_latency.log
```

`main.py` enables line buffering so application prints reach the journal promptly even
when Python runs behind a systemd pipe. Timing measurements also persist in the rotating
`chat_latency.log` file. See [conversation checks](CONVERSATION_CHECKS.md) for stage names
and how to distinguish queue wait, routing, provider generation, and first delivery.

Source edits take effect when the bot process next starts. Compare the service's start
time with when changes were made before assuming a test exercised current code.
Do not restart or edit neighboring services as part of a read-only investigation.

The October 13 investigation found the bot running since October 12 at 23:13 Los Angeles
time, before the diary and detailed timing changes. The journal had Discord connection
logs but no application chat/timing prints, and the process used neither `-u` nor
`PYTHONUNBUFFERED`. This prevented recovering phase timings for the earlier greeting.
The code path did require DeepSeek to finish before starting the remaining characters;
explicit casual group invitations now use concurrent opening generation.

The later October 13 video investigation found a separate live-delivery bug: an incoming
DM could be absent from Discord's channel cache. The journal showed repeated
`Failed to start agent progress` and `Failed to send live room turn` errors even though
final responses reached the native DM channel. The adapter now remembers incoming
channels and fetches uncached ones. See [the architecture and incident analysis](ARCHITECTURE.md)
for the shared room loop and structured tool failure handling.
