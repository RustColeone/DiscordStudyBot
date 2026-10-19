# `basic`: the current time and simple notes

A built-in module with three capabilities: the date and time (optionally in another time zone), saving
a short note, and listing saved notes. People use them as `$now`, `$remember` and `$notes`; engines
offer them to models as tools. It is the smallest complete module in the project, so read it first if
you want to write a capability: one file, three handlers, one `setup`.

## What it adds

| Kind | Name | Tier | Access | `$command` | Notes |
| --- | --- | --- | --- | --- | --- |
| Capability | `basic.now` | read | member | `$now [timezone]` | Optional `timezone` (IANA name) |
| Capability | `basic.remember` | write | member | `$remember <text>` | Required `text`; asks the owner by default |
| Capability | `basic.notes` | read | member | `$notes` | No parameters |

No interfaces, engines, personas, profiles, defaults or declared settings. The core's built-in
permissions limit routine conversations (`routine:*`) to `basic.*`, so these are the capabilities a
configured routine can use out of the box (`core/defaults.py`).

## Setup

Nothing to configure: no `config.example.yml`, no `.env` variables, only the standard library. It
loads with the other built-in modules. To turn it off, add it to core.yml:

```yaml
modules:
  exclude: [extensions/basic]
```

## Using it

```
$now                      2026-10-19 14:03:00 Monday CST (UTC+0800)   (the bot machine's zone)
$now Europe/Berlin        the same moment in Berlin
$remember buy milk        Saved note #1.
$notes                    1. buy milk
```

* `$now` with an unknown zone answers `Unknown IANA timezone 'X'` (error code `invalid_timezone`).
* `$remember` is a write: when a member (not an owner) types it, or a model calls it, the owner is
  asked first. An owner's own `$remember` counts as the approval and runs at once. Text is cut to
  2,000 characters; empty text fails with `empty`.
* `$notes` shows the latest 20 notes saved in this conversation. Each conversation keeps its own 200
  latest; another conversation's notes are never shown.
* Notes saved before notes were kept per conversation (one shared list) are shown to the creator only,
  under their own heading.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | The three handlers, `setup` returning their `Capability` declarations, and `EXTENSION` |

Each handler is `async def handler(arguments, context) -> ToolResult`. The core validates `arguments`
against the declared `parameters` before calling it, so `remember` can read `arguments["text"]`
without checking that it exists. Expected failures are returned with `ToolResult.fail(summary,
error_code)`, never raised. `data=` carries machine-readable output (`iso` and `timestamp` for `now`,
the notes for `notes`) next to the human-readable summary.

`remember` and `notes` use `context.storage`, the module's private key-value store. The store belongs to
the module, not to a conversation, so the handlers key it by conversation themselves: `notes:<address>`
(`context.address`). The older shared list stays under `notes` and is read only for the creator
(`context.access`).

Safety comes entirely from the declared tiers: `basic.now` and `basic.notes` are reads and run without
asking; `basic.remember` is a write, which the core's default permissions make ask the owner.
core.yml can relax it, for example `permissions: {"*": {rules: {basic.remember: auto}}}`.

## Data it keeps

The `notes` list in the core's store for this module (`tank.db` in the core's data directory): each
note's `text`, `at` (epoch seconds) and `by` (the sender's name or ID). At most 200 notes; older ones
are dropped when a new one is saved. There is no command to delete notes.

## Tests

The module has no test file of its own: [`tests/core_support.py`](../../tests/core_support.py) loads
it into every core test, and these tests use it:

* `tests/test_core_runtime.py`: `$now UTC`; a member's `$remember` waits for approval, then the owner's
  `$approve` saves it and `$notes` lists it; a decision cut off by a restart is not repeated.
* `tests/test_core_routines.py`: a typed `$routine ... $remember` runs as the creator's approval, one a
  model set up asks.
* `tests/test_core_permissions.py`: `use: ["basic.*"]` patterns; a module may not suggest rules for
  `basic.remember`.

```
venv/bin/python -m unittest tests.test_core_runtime tests.test_core_routines tests.test_core_permissions
```

## Building something similar

* Declare a capability with name, description, handler, parameters, required, tier and command
  ([`extension.py:59-68`](extension.py)). The name must be `<module name>.<action>`.
* A plain `command="now"` with `command_argument="timezone"` is enough for a `$command`: the text
  after `$now` becomes that parameter ([`extension.py:62`](extension.py)). Use a `Command` declaration
  when you need several options (see [`reminders`](../reminders/README.md)).
* Give a parameter a `description` (`timezone`, line 44): models read it, and native command menus
  use it as the option's help.
* Read and write state with `context.storage.get` / `set` ([`extension.py:36-39`](extension.py)) and
  bound its size yourself (`notes[-200:]`).
* Pitfall: `context.storage` is one store per module. If data belongs to a person or a conversation,
  key it by `context.address` or `context.sender.id`, and filter on read.
* Pitfall: declare the tier honestly. A capability that changes anything is at least `write`.

See also: [Capabilities](../../docs/core/EXTENSIONS.md#capabilities) and
[Declaring commands](../../docs/core/EXTENSIONS.md#declaring-commands) in the module guide,
[Permissions](../../docs/core/ARCHITECTURE.md#permissions) in the architecture,
[`system`](../system/README.md), [`reminders`](../reminders/README.md).
