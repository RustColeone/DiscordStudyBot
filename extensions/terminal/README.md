# `terminal`: a creator-only shell on the bot's machine

`terminal` gives the bot's creator a shell on the computer the bot runs on (PowerShell on Windows, bash
on Linux), one long-lived shell per conversation. There are three ways in: the `$t` command, a
conversation assigned to the `terminal` engine where every message is a shell line, and the assistant,
which may type lines only with the creator's approval of each one. Study it for two things: a module
that is both an engine and capabilities sharing one object, and the most dangerous capability in the
project. A shell line can do anything the bot's OS user can do, including reading `.env` and sending it
elsewhere. The core's secret filters do not cover what a line does by itself. The guarantees are the
creator's approval of the exact line and the operating system's limits on the bot's user.

## What it adds

| Capability | Tier | Access | Command | Attributes | Timeout |
| --- | --- | --- | --- | --- | --- |
| `terminal.send` | `critical` | `creator` | `$t [input]` | `never_auto`, `describe` ("Shell line: ...") | `max_wait_s` + 30 s |
| `terminal.peek` | `read` | `creator` | none | `private` | 30 s |

Neither sets `subject`, `confirm`, `grantable` or `escalate`.

| Other contribution | Value |
| --- | --- |
| Engine | `terminal` (`EngineSpec("terminal", ...)`, one persona): every message is a shell line |
| Persona | `terminal` ("Terminal"): names the speaker; no model is used |
| Declared settings | `cwd` (kind `path`, default `~`), `max_wait_s` (`number`, 20), `idle_minutes` (`number`, 60); no env variables |
| Default permissions | none; the core's defaults apply |

## Setup

The module needs `psutil` (in `requirements.txt`) and a shell. By default it uses `pwsh`, else
`powershell` (`-NoLogo -NoProfile -NonInteractive -Command -`) on Windows, and `bash --norc --noprofile
--noediting -i` elsewhere, or `/bin/sh` without bash. Settings go in `extensions/terminal/config.yml`
(copy [`config.example.yml`](config.example.yml); git-ignored) or under `extension_config.terminal` in
`core.yml`:

```yaml
# shell: [powershell.exe, -NoLogo, -NoProfile, -NonInteractive, -Command, "-"]   # default: PowerShell / bash
cwd: ~                          # where new shells start
quiet_s: 1.5                    # reply once nothing new has appeared for this long
max_wait_s: 20
update_s: 3                     # how often the output in progress is refreshed
idle_minutes: 60                # shells end after this long without input
tail_chars: 1800
```

| Key | Meaning |
| --- | --- |
| `shell` | The shell's command line, as a list or a single program name. On Linux use an interactive shell, or `^C` ends it. |
| `cwd` | The folder new shells start in (default: home). |
| `quiet_s` | Seconds of silence after which the reply is sent. |
| `max_wait_s` | The longest wait for output after a line before replying; the program keeps running. |
| `update_s` | How often the progress message with the output so far is refreshed. |
| `idle_minutes` | A shell unused this long is ended (see [Using it](#using-it)). |
| `tail_chars` | How much of the end of the output a reply shows. |

The module uses no `.env` variables. It loads with the built-in modules unless `core.yml` excludes it.
The exclusion example in [`core.example.yml`](../../core.example.yml) and this repository's own
deployment list it:

```yaml
modules:
  exclude: [extensions/terminal]    # or: terminal
```

Remove `extensions/terminal` from that list to enable it, add it to turn the module off. Excluded
modules are never imported. For a terminal conversation, assign the engine and admit only the owner:

```yaml
assignments:
  "discord:123456789012345678": {engine: terminal, persona: terminal}
admission:
  "discord:123456789012345678": {require_mention_in_groups: false, owner_only: true}
```

Before enabling it, decide what the bot's OS user may reach: run the bot as its own user, keep file
permissions tight, and see [Enforcement: secrets](../../docs/core/ARCHITECTURE.md#enforcement-secrets).

## Using it

```
$t df -h                  a line for this conversation's shell
$t                        output that appeared since it was last shown
$t ^C                     interrupt the running program; the shell stays
```

* In a terminal conversation, plain messages are lines. A line starting with `$` and a letter is read as
  a command, so send it as `$t $HOME`.
* Asking the assistant ("how much disk is free?") makes it call `terminal.send`. The creator gets an
  approval request showing `Shell line: df -h` and answers `$approve <id>` or `$deny <id>`. In the creator's
  private chat the model may also call `terminal.peek` to see further output without asking.
* While a line runs, a progress message shows the output so far (prefixed `running…`), refreshed every
  `update_s`. The reply comes once nothing new has appeared for `quiet_s`, or after `max_wait_s`. It
  shows the last `tail_chars` characters (fewer if the conversation's text limit is smaller), with `…`
  when cut, and names up to three programs still running.
* Lines the same person types in a terminal conversation meanwhile go straight to the shell.
  Cancelling the run (`$cancel`, or the progress message's Cancel button where the interface has one)
  interrupts the program.
* `cd` and variables persist. There is no terminal: full-screen programs (`vim`, `top`) and password
  prompts (`sudo`) do not work. The shell gets `TERM=dumb`, `NO_COLOR=1`, `PAGER=cat`, `GIT_PAGER=cat`
  and `PYTHONUNBUFFERED=1`; colour codes are removed and a progress bar shows its last state.
* `^C` on Linux sends SIGINT to the shell's process group. On Windows the running programs are ended;
  with none (a loop in PowerShell itself) the shell is ended and the next line starts a new one.
* A shell that exited is replaced on the next line, and the reply says so. A shell unused for
  `idle_minutes` is ended the next time any other conversation's shell is used; all shells end when the
  bot exits.
* If the bot restarts while a terminal-conversation line runs, it says the line was not sent again.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION` and its settings, `TerminalEngine`, the `terminal` persona, `terminal.send` and `terminal.peek` |
| [`shell.py`](shell.py) | `Shell` (one process, its output buffer, interrupt and kill) and `Terminals` (one shell per conversation, the `exchange` loop, rendering) |
| [`config.example.yml`](config.example.yml) | The documented settings |

`setup` builds one `Terminals` and gives it to the engine and both capabilities, keyed by the
conversation's address, so `$t` and a terminal conversation share a shell. Each shell is started with
plain pipes (stdout and stderr merged) in a new session on POSIX or a new process group on Windows, so
an interrupt reaches the shell and its children, never the bot. Fresh shells get quiet settings (bash:
no prompts, no history expansion; PowerShell: UTF-8 output), then `echo __terminal_ready__` so the
startup banner is skipped. A reader task keeps the last 200,000 characters; positions are absolute
counts, so each exchange shows exactly what appeared since the last one.

`Terminals.exchange` writes the line, polls, sends lines from `more()` as they arrive, and reports
progress; on cancellation it interrupts the shell and re-raises. `render` keeps only the tail and
replaces triple backticks so the output cannot close its code block. A lock per conversation makes simultaneous first
lines share one shell.

### Safety design

There are two paths to the shell, and they are guarded differently.

**Through capabilities** (`$t` and models). These go through the core's three layers
([Permissions](../../docs/core/ARCHITECTURE.md#permissions)):

* **Access.** Both capabilities are `access="creator"`; anyone else is refused and models in their
  runs are not offered them. `terminal.peek` is `private`, so a model sees it only in a conversation
  private to the owner, and a run that used it stays in its conversation or the control route.
* **`critical` + `never_auto`.** Every model call to `terminal.send` asks the creator; no tier setting,
  rule or grant changes that, and "always allow" is never offered (it is also not `grantable`). The
  prompt, written by the core from `describe`, shows the exact line, and `$approve` runs that line once.
* **Typed commands.** The creator's own `$t` is their approval, so it runs at once. A `$t` line holding
  a known secret value asks to confirm first. A model's line holding one is refused and cannot be
  approved.
* **Routines.** A `$t` in a routine the creator typed runs as theirs. One fired by a routine a model
  set up (`routine_via == "tool"`) is not the creator's approval and asks.
* **Restarts.** A critical action interrupted by a crash is never repeated
  (`uncertain_previous_attempt`).

**Through the engine** (a terminal conversation). Messages there are not capability calls, so the
Approval layer does not apply; the engine checks for itself (`extension.py:39-59`): a resumed run
sends nothing, only `session.access == "creator"` gets through, a routine a model set up is refused,
and lines typed meanwhile count only from the same sender and not from such routines. The admission
rule `owner_only: true` adds a second gate at the gateway.

What neither path covers: the shell runs as the bot's OS user with the bot's environment, which
includes variables loaded from the env files. Output that passes through the bot is scrubbed of known
secrets, but a line that reads or sends them itself is not stopped by the core.

## Data it keeps

Nothing on disk. Each shell's last 200,000 characters of output are kept in memory until the shell
ends. `terminal.send` logs a `terminal_input` audit event with the address and the line's length, not
its text. The core keeps its usual records: history, the action log with the scrubbed line, and
approvals.

## Tests

[`tests/test_core_terminal.py`](../../tests/test_core_terminal.py) (POSIX only):

* `ShellTests` drives `Terminals` directly: a shell per conversation, progress while a line runs,
  `^C` keeping the shell (also typed meanwhile, for a loop in the shell itself), later output on a
  second look, an exited shell replaced, tail rendering and progress bars, one shell for simultaneous
  first lines, and a failed start retried.
* `TerminalCoreTests` runs a real core: only the creator reaches the shell by `$t` or a terminal
  conversation; a model-made routine cannot type into one; a model's `terminal.send` waits for
  approval and runs after `$approve`; `$t` and the terminal conversation share a shell; cancelling a
  `$t` run interrupts the program.

```
venv/bin/python -m unittest tests.test_core_terminal
```

## Building something similar

* For anything that acts on the machine, declare `risk=Risk.CRITICAL`, `never_auto=True` and a
  `describe` that shows the exact input (`extension.py:93-101`).
* Keep the read side separate and `private` (`terminal.peek`, `extension.py:102-104`).
* An engine that bypasses capabilities must do its own checks: `session.resumed`, `session.access`,
  model-made routines (`extension.py:28-30`, `39-59`), and filter `session.inbox()` by sender.
* Never re-send on resume: tell the person instead (`extension.py:40-43`).
* Start child processes in their own session or process group so signals stay with them
  (`shell.py:80-84`); kill them on exit (`atexit`, `extension.py:70`).
* Make cancellation stop the work (`shell.py:255-257`).
* Log that something happened, not its content (`extension.py:74`).
* Pitfall: the core's secret filters stop at the bot's boundary. Say so in your module and rely on OS
  isolation for what the process can reach.
* Pitfall: only `terminal.send` writes the audit event; lines in a terminal conversation do not.

See also: [EXTENSIONS.md](../../docs/core/EXTENSIONS.md) ([the terminal](../../docs/core/EXTENSIONS.md#the-included-terminal),
[Engines](../../docs/core/EXTENSIONS.md#engines), [Routine examples](../../docs/core/EXTENSIONS.md#routine-examples)),
[ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md) ([Permissions](../../docs/core/ARCHITECTURE.md#permissions),
[Durable runs](../../docs/core/ARCHITECTURE.md#durable-runs)), [`host`](../host/README.md) for the screen,
files and clipboard.
