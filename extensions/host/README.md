# `host`: the creator's screen, files, clipboard and apps

`host` lets the bot's creator reach the computer the bot runs on from any chat: take a screenshot, fetch a
file, save attachments onto the disk, read or set the clipboard, open a URL or file on the screen, and
see what is running. It works on Windows, macOS and Linux. Study it as the project's example of a
module that handles private data: every capability is creator-only, everything that only looks is a
`private` read, and the one action that writes to the disk is `critical`. The risks are real: a
screenshot or a file can expose anything on the machine, and saving a file can put something
executable on it. The core's permissions keep that in the creator's hands; this module declares the
attributes that make that happen.

## What it adds

| Capability | Tier | Access | Command | Attributes | Timeout |
| --- | --- | --- | --- | --- | --- |
| `host.screenshot` | `read` | `creator` | `$screenshot` | `private`, needs `files` | 60 s |
| `host.get_file` | `read` | `creator` | `$getfile <path>` | `private`, `subject="path"`, `subject_kind="path"`, needs `files` | 120 s |
| `host.save_files` | `critical` | `creator` | `$savefile [folder]` + attachment | `subject="folder"`, `subject_kind="path"`, `describe` | 120 s |
| `host.read_clipboard` | `read` | `creator` | `$clipboard` | `private` | 30 s |
| `host.write_clipboard` | `write` | `creator` | `$copy <text>` | none | 30 s |
| `host.open` | `write` | `creator` | `$open <target>` | none | 30 s |
| `host.processes` | `read` | `creator` | `$processes [sort]` | `private` | 30 s |

No capability sets `never_auto`, `confirm`, `grantable` or `escalate`. `$screenshot`, `$savefile` and
`$clipboard` are declared as `Command(..., access="creator")`; the others are plain names, and the core
raises a command's level to its capability's in the catalog. Either way nobody but the creator sees
them in `$help` or native menus.

| Other contribution | Value |
| --- | --- |
| Engines, personas, interfaces | none |
| Declared settings | `downloads_dir` (kind `path`, default `~/Downloads`, no env variable) |
| Default permissions | none; the core's defaults apply (reads run, writes and critical actions ask, `routine:*` may use only `basic.*`) |

## Setup

Python packages: `psutil` and `pillow` (both in `requirements.txt`). Settings go in
`extensions/host/config.yml` (copy [`config.example.yml`](config.example.yml); git-ignored) or under
`extension_config.host` in `core.yml`, which overrides it:

```yaml
downloads_dir: ~/Downloads      # where $savefile puts attachments by default
desktop_env: {}                 # Linux: the desktop session, found by itself; e.g. {DISPLAY: ":1"}
```

* `downloads_dir`: the default folder for `host.save_files`. Also settable through `python -m core
  setup` and `$setup`, since it is a declared `Setting`.
* `desktop_env`: environment variables merged last over what the module works out for the desktop
  session (below). Only needed when the guess is wrong.

The module uses no `.env` variables. What it needs from the system depends on the platform:

| Platform | Screenshot | Clipboard | Open |
| --- | --- | --- | --- |
| Windows | Pillow `ImageGrab` (all screens) | `powershell` `Get-Clipboard` / `Set-Clipboard` | `os.startfile` |
| macOS | Pillow `ImageGrab` | `pbpaste` / `pbcopy` | `open` |
| Linux, Wayland | `grim` (wlroots: sway, labwc, wayfire, the Raspberry Pi desktop), `gnome-screenshot` (GNOME) or `spectacle` (KDE) | `wl-clipboard`, then `xclip`, then `xsel` | `xdg-open` |
| Linux, X11 | `scrot`, then Pillow `ImageGrab` on `DISPLAY` | `xclip`, then `xsel` | `xdg-open` |

A bot started as a service has no desktop variables. On Linux the module fills them in: `XDG_RUNTIME_DIR`
(default `/run/user/<uid>`), `WAYLAND_DISPLAY=wayland-0` when that socket exists, `DISPLAY=:0` when unset,
and `DBUS_SESSION_BUS_ADDRESS` from `$XDG_RUNTIME_DIR/bus`. Variables already set are kept;
`desktop_env` overrides all of them.

The module loads with the other built-in modules. To turn it off, exclude it in `core.yml`:

```yaml
modules:
  exclude: [extensions/host]      # or: host
```

## Using it

The creator types commands in any conversation, or asks the assistant in plain words ("send me a
screenshot", "what is eating memory?").

```
$screenshot                       the whole screen, as an image
$getfile Documents/report.pdf     a file; absolute, or relative to the home folder
$savefile                         with one or more attachments: saved to downloads_dir
$clipboard                        the clipboard's text
$copy meeting at 3 in room 204    put text on the clipboard
$open https://example.com         a URL in the browser; also a file or folder with its default app
$processes memory                 the busiest processes, by cpu (default) or memory
```

* `host.screenshot` sends a PNG, or a JPEG (quality 80, scaled down in steps to a quarter) when the PNG
  is over the conversation's upload limit. If even that is too large it fails.
* `host.get_file` refuses folders (it suggests the terminal) and files over the upload limit
  (`too_large`, with the size). It sends a copy taken at that moment.
* `host.save_files` saves only attachments on the creator's own messages in the run, optionally only
  those listed in `names`. Names that are not among them fail with the list of what is attached. A
  name already in the folder gets ` (1)`, ` (2)`, ... The folder is created if missing; a relative one is
  under the home folder.
* `$savefile <folder>` saves into that folder (`command_argument="folder"` passes the typed text on);
  without one, files go to `downloads_dir`.
* `host.read_clipboard` shows at most 4000 characters, or says the clipboard has no text.
* `host.open` takes a target starting with `http:`, `https:` or `mailto:`, or an existing file or
  folder (relative to home). Anything else, such as `javascript:...`, fails with `not_found`. It does not
  wait for the app.
* `host.processes` samples CPU over half a second. Models may also pass `count` (default 10, at most 30).

If a desktop tool is missing or fails, the result names the tools it tried and each error
(`desktop_unavailable`).

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION`, the `downloads_dir` setting, the seven handlers and their `Capability` declarations |
| [`desktop.py`](desktop.py) | The platform layer: `Desktop` (screenshot, clipboard, open), `session_env`, `free_name`, `DesktopError` |
| [`config.example.yml`](config.example.yml) | The documented settings |

`setup` builds one `Desktop` from `desktop_env` and returns `Contributions(capabilities=[...])`. Each
handler gets validated arguments and a `ToolContext`; it writes output files into `tool.workspace`,
sizes them with `tool.display.max_upload_mb`, formats with `tool.display.markdown`, and returns a
`ToolResult` (files as `FileRef`). Expected failures are `ToolResult.fail` with a code, never
exceptions. The core delivers the files and scrubs secrets from the result.

`Desktop` tries each platform's usual tools in order and collects the failures for one plain error.
Two choices are worth knowing:

* Under Wayland it never falls back to X11 capture, which would only see XWayland and return a black
  frame (`desktop.py:102-121`).
* `wl-copy` and `xclip` exit but leave a child serving the clipboard with the pipes open. Clipboard
  writes therefore run with `capture=False` and wait only for the tool itself (`desktop.py:56-88`,
  `170-171`). PowerShell is told to read and write UTF-8.

`open` starts the app in a new session and returns at once. `processes` runs its half-second sample in
a thread so the event loop is not blocked.

### Safety design

The core decides every call in three layers (see [Permissions](../../docs/core/ARCHITECTURE.md#permissions)).
This module's declarations feed them as follows.

* **Access.** Every capability is `access="creator"`. Members' and admins' commands are refused, and
  models in their runs are not offered the tools at all.
* **`private` reads.** Screenshot, file, clipboard and process list are `read`, so they run without
  asking, but `private`, so they are offered to a model only in a conversation private to the owner
  (their direct chat with a bot, or the control route). A run that used one delivers only to its own
  conversation or the control route, and any critical action later in that run asks, whatever the rules
  or grants say. `host.write_clipboard` and `host.open` show nothing back, so they are not `private`.
* **Path subjects.** `host.get_file` (`path`) and `host.save_files` (`folder`) declare
  `subject_kind="path"`. A model's call naming a secret path (`.env`, private keys, `~/.ssh/*`, cloud
  credentials, modules' `config.yml`, the core's databases, `secrets:` in `core.yml`) is refused, cannot
  be approved, and the owner is told. The core resolves a relative path against home, as the handlers
  do, and also checks where a symlink leads. The check covers only the subject string: a `folder` is
  checked as a folder, not the names of the files written into it.
* **Tiers.** Saving onto the disk is `critical`: a model's call asks. No tier setting or pattern makes
  it automatic; only a rule naming `host.save_files` with one exact folder could. `host.open` and `host.write_clipboard` are `write`: they ask by default,
  and a rule such as `host.open: auto` may change that.
* **What the approval shows.** `host.save_files` saves only `tool.own_files`, the attachments of the
  person who started the run, never files other people added to a group run. Its `describe` names the
  files and the folder, so the approval shows exactly what will be written.
* **Not grantable.** No capability is `grantable`, so "always allow" is never offered; each model call
  that asks is approved one at a time (`$approve <id>`).
* **Typed commands.** The creator's own `$command` is their approval and skips the `use` list and
  `private`. It still shows a confirmation when it carries a secret (`$getfile ~/.ssh/id_ed25519`
  asks before sending the key out) or shows private data in a conversation others can read
  (`$screenshot` in a group channel). A `$command` from a routine a model set up is not the creator's
  approval.

## Data it keeps

Nothing of its own: no store and no files in `context.data_dir`. Screenshots and file copies go to the
run's workspace, which the core deletes at startup 7 days after the run ended
([Retention](../../docs/core/EXTENSIONS.md#retention)). Saved attachments stay where they were saved.
The core keeps its usual records: history, the action log with scrubbed arguments, and approvals.

## Tests

[`tests/test_core_host.py`](../../tests/test_core_host.py):

* `CapabilityTests` calls the handlers with a fake desktop: the tiers, `private`, path subjects and
  creator-only access; screenshots shrunk to fit; files sent as snapshots and saved without
  overwriting; only the creator's own named attachments saved; clipboard, `open` (refusing
  `javascript:`) and the process list; the session environment.
* `DesktopTests` runs the real `Desktop` against stub tools on `PATH` (POSIX only): a background
  clipboard writer does not hang, and Wayland screenshots never fall back to X11.
* `HostPolicyTests` runs a real core: a model's `host.open` waits for approval, members are refused,
  a private read runs in the creator's own chat, and every command is hidden from non-creators.

```
venv/bin/python -m unittest tests.test_core_host
```

## Building something similar

* Declare `access` on the capability (`extension.py:163-194`); a `Command` may repeat it, as
  `$screenshot` does, so the declaration reads plainly. The command is then hidden from menus as well
  as refused.
* Split looking from changing. Make what shows private data a `private` read, and keep writes out of
  `private` unless they also show something.
* Give every path argument `subject_kind="path"` so the core's secret-path check applies
  (`extension.py:170`, `175`). Resolve relative paths the same way the core does (against home).
* Anything that writes from attachments should use `tool.own_files`, never `tool.files`, and take the
  names to write, with a `describe` that lists them (`extension.py:80-102`, `176-177`).
* Hand out snapshots, not live paths: copy into `tool.workspace` (`extension.py:76-77`).
* Check URL schemes against an allow-list before handing a string to the desktop (`extension.py:25`,
  `119-128`).
* Pitfall: a `Command` with options still needs `command_argument` for typed text to reach a
  parameter; without one the text is dropped, as with `$savefile`'s folder.
* Pitfall: a `path` subject protects the path it names. When the call writes inside a folder, keep the
  tier `critical` so each call is approved.
* Pitfall: tools that fork a background child (clipboard owners) hold captured pipes open; do not wait
  on their output.

See also: [EXTENSIONS.md](../../docs/core/EXTENSIONS.md) ([Capabilities](../../docs/core/EXTENSIONS.md#capabilities),
[the host module](../../docs/core/EXTENSIONS.md#the-included-host-module)),
[ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md) ([Permissions](../../docs/core/ARCHITECTURE.md#permissions),
[Enforcement: secrets](../../docs/core/ARCHITECTURE.md#enforcement-secrets)),
[`terminal`](../terminal/README.md) for shell work such as listing folders or ending processes.
