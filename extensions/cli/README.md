# `cli`: chat with the bot on its own console

A built-in module that turns the terminal running `python -m core` into one conversation with the bot:
type a line, and replies are printed back. Use it for local testing without Discord, as a console of
last resort for approvals on a machine with nothing else connected, or as the smallest complete
interface to read before writing your own (one file, about 110 lines).

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `cli` | `Display(markdown=True, edits=False, progress=True, quotes=False, files=True, max_upload_mb=1024, max_text_chars=100_000)`. One conversation, `cli:<conversation>` (default `cli:local`). No `operations`. |
| Owner report | `port.set_owners([user], control=cli:<conversation>, fallback=True)` | Only when stdin is an interactive terminal. Used only when core.yml lists no `cli` owners, and the console becomes the control route only when no interface offers a real one ([ARCHITECTURE.md, Owners and control](../../docs/core/ARCHITECTURE.md#owners-and-control)). |

No engines, capabilities, personas, profiles, assignments, admission or permissions: its conversation
follows core.yml's `*` rules (by default the `assistant` engine).

Declared setting: `user_name` (text, default `you`; no env var).

## Setup

It loads by default. To change it, copy [`config.example.yml`](config.example.yml) to `config.yml`
here (git-ignored); `extension_config.cli` in core.yml overrides it.

| Key | Default | Meaning |
| --- | --- | --- |
| `conversation` | `local` | The conversation part of the address (`cli:local`). |
| `user` | `local` | Your sender ID. Add it to `owners: {cli: [local]}` in core.yml to be the owner when stdin is not a terminal (piped input, a service). |
| `user_name` | `you` | Your display name. |

No `.env` variables and no third-party packages. To turn it off (a service with no console, say):

```yaml
modules:
  exclude: [cli]            # or extensions/cli
```

## Using it

Start the bot in a terminal (`venv/bin/python -m core`; the supervisor passes stdin and stdout through to
the bot). At an interactive terminal it prints `[cli] You are the creator at this console...`. Type a
line and press Enter; blank lines are ignored, and `$help` and the other `$commands` work as anywhere.
Replies look like:

```
Luna> Hello!
Luna · Work note> I will look it up first.
  ... <a run's progress line>
bot> [file notes.txt: /path/to/notes.txt] caption
[waiting for the owner's approval 1a2b3c]
[approval_request] 🔐 Approval 1a2b3c · WRITE  (more lines, ending in "Reply $approve 1a2b3c or $deny 1a2b3c.")
```

There are no buttons: approvals and other actions are answered by typing the `$command` their text
shows. Files are not copied; the line shows the local path. Ending stdin (Ctrl-D) stops reading.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `CLIInterface(Interface)`, its `NOTICES` phrasing, `setup` and `EXTENSION`. |
| [`config.example.yml`](config.example.yml) | The three settings above. |

* `start(port)` (extension.py:35) stores the port, reports the owner if stdin is a TTY, and starts the
  read loop as a task, returning at once; `stop()` cancels that task.
* `_read_loop` (extension.py:52) reads stdin on a daemon thread and hands lines to the event loop with
  `call_soon_threadsafe`. A daemon thread, because a pending console read must never keep the process
  from exiting on restart or Ctrl-C. Each non-blank line becomes `port.submit(Request(...))` with ID
  `<time_ns>-<counter>`, `Address("cli", conversation)`, `Sender(user, user_name)` and `display`. On
  end of input it logs `stdin_closed` and stops.
* `deliver(reply)` (extension.py:79) prints one line per reply: `text` as `speaker> text` (`bot` when
  there is no speaker), `file` as its path, `progress` indented with `...`, notice `reminder` with ⏰,
  the codes in `NOTICES` (`run_failed`, `waiting_approval`, `unknown_command`, `not_permitted`,
  `locked`, `relayed`) as short bracketed phrases, and anything else (including approval requests and
  events) as `[<code, or notice>] text`. Routed replies are prefixed with `[<origin_address>]`. It
  always returns `Delivery(True, message_id="out-...")`.

Safety: it trusts whoever has the console (who can edit the bot's files anyway), and claims ownership
only at an interactive terminal; piped input is an ordinary sender unless core.yml says otherwise.

## Data it keeps

Nothing. The core keeps the conversation's history as for any interface.

## Tests

`ConsoleTests` in [`tests/test_core_setup.py`](../../tests/test_core_setup.py): at a TTY the console
reports itself as owner with a fallback control route and prints the creator banner, piped input reports
nothing, and a fallback control route never replaces a real one.
[`tests/test_core_discord.py`](../../tests/test_core_discord.py) checks that `cli` loads by default and
that `modules: {exclude: [cli]}` turns it off.

```bash
venv/bin/python -m unittest tests.test_core_setup.ConsoleTests
```

## Building something similar

* The minimum interface is `name`, `display`, `start`, `stop` and `deliver` (extension.py:22-99).
* Never block the event loop on a platform read: use a thread or an async client, and pass results back
  with `loop.call_soon_threadsafe` (extension.py:56-66).
* Request IDs must be unique and stable; a platform message ID is better than the time-based one used
  here, because the core deduplicates by ID across restarts.
* Phrase notices by `reply.code`, falling back to `reply.text`; without buttons, the text already names
  the `$command` to type.
* `setup` only builds objects; the loop starts in `start` (extension.py:102-106).

See also: [docs/core/EXTENSIONS.md, Interfaces](../../docs/core/EXTENSIONS.md#interfaces),
[docs/core/ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md),
[`plugins/webui`](../../plugins/webui/README.md) (a full interface with a server and a page),
[`extensions/discord`](../discord/interface.py).
