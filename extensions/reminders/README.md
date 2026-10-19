# `reminders`: one-time reminders in the conversation that asked

A built-in module that lets anyone set a reminder ("in 10 minutes, drink water"), list the pending
ones and cancel their own. At the set time the core posts the text back into the same conversation.
Models use the same capabilities when someone asks in plain words. Study it for a `Command` with
several options and for `context.schedule`, the core's durable timer.

## What it adds

| Kind | Name | Tier | Access | `$command` | Notes |
| --- | --- | --- | --- | --- | --- |
| Capability | `reminders.create` | write | member | `$remind <when> <text>` | Parameters `text`, `at`, `in_minutes`, `spec` (none required) |
| Capability | `reminders.list` | read | member | `$reminders` | No parameters |
| Capability | `reminders.cancel` | write | member | `$unremind <id>` | Required `id` |

No interfaces, engines, personas, profiles or declared settings. Although `create` and `cancel` are
writes, the core's built-in permissions make both `auto` for everyone (`BASIC_FEATURES` in
`core/defaults.py`): a reminder only affects the conversation it was set in, so nobody waits for the
owner. Where a conversation's write tier is `always-ask` (as in `whale_chan`'s), that rule does not
apply and members' reminders ask too.

## Setup

Nothing to configure: no `config.example.yml`, no `.env` variables, only the standard library. To turn
it off:

```yaml
modules:
  exclude: [extensions/reminders]
```

## Using it

```
$remind 10m drink water          Reminder 3f9a1c set for 2026-10-19 14:13 CST: drink water
$remind 2h30m stretch
$remind 1 day 2 hours plan the week
$remind 18:30 call mom           today at 18:30, or tomorrow if that has passed
$remind 2026-10-20T08:00+08:00 dentist
$reminders                       3f9a1c · 2026-10-19 14:13 CST · drink water (Alice)
$unremind 3f9a1c                 Reminder 3f9a1c cancelled.
```

* `when` is one or more durations (`s`/`sec`/`second`, `m`/`min`/`minute`, `h`/`hr`/`hour`,
  `d`/`day`, the longer ones also plural; decimals allowed), a clock time `HH:MM`, or an ISO 8601 time
  written without spaces. Clock times and ISO times without an offset use the bot machine's time zone.
* `$reminders` lists every pending reminder in this conversation, whoever set it. Only the person who
  set a reminder can cancel it.
* Limits come from the core: the time must be in the future and within a year, at most 50 pending per
  conversation, text up to 2,000 characters.
* Discord's slash form `/remind` has two required options, `when` and `text`.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | Time parsing (`parse_spec`, `parse_iso`), the three handlers, `setup` and `EXTENSION` |

There are two ways to say when. A model is told to send `text` plus either `at` (ISO 8601 with an
offset, which it computes from the current time) or `in_minutes`; a person's `$remind` text arrives
whole in `spec` and `parse_spec` splits it into a time and the text. Bad input raises `ValueError`
inside the handler, which turns it into `ToolResult.fail(..., "invalid_arguments")` with a message
that shows the accepted forms.

The module stores nothing itself. `context.schedule(at, text)` hands the reminder to the core, which
keeps it in its gateway database, survives restarts and, when the time comes, delivers it to the
conversation as a `notice` with code `reminder` (`data` has `scheduled_id`, `sender_id`, `sender_name`
and `at`).
Each interface phrases that notice its own way. `context.scheduled()` and `context.unschedule(id)` only
see the calling conversation's items, so `list` and `cancel` cannot reach other conversations.

`cancel` checks `item["sender_id"] != context.sender.id` itself: access levels decide who may call a
capability, the handler decides whose items it may touch. An admin or the creator may cancel anyone's
(`context.access`). A routine gets no exception: it acts as the person who set it up (an owner's
trusted routine as the creator), so a member's routine cannot cancel someone else's reminder.

## Data it keeps

Nothing in the module. Pending reminders live in the core's `gateway.db` (address, time, text, who set
it). Sent, cancelled and failed ones are deleted at startup 30 days after their time; a delivered
reminder is also recorded in the conversation's history.

## Tests

`tests/test_core_reminders.py`, inside a real core: `$remind` and a model's call both set reminders,
`$reminders` lists them in time order with their setters, and the due one fires as a `reminder` notice
in the same conversation; only the setter can cancel; a time in the past, an unparseable time and a
call without a time are explained; `parse_spec` handles combined durations, `HH:MM` and ISO times.

```
venv/bin/python -m unittest tests.test_core_reminders
```

## Building something similar

* A `Command` with typed options and a `template` ([`extension.py:101-105`](extension.py)) gives native
  menus two fields while the handler still receives one text, via `command_argument="spec"` (line 104).
* Accept both a model-friendly form and a person-friendly form in one capability
  ([`extension.py:50-70`](extension.py)), and say in the description which one models should use.
* Use `context.schedule` instead of your own timer ([`extension.py:66`](extension.py)): it is durable,
  bounded and delivered through the core's policy.
* Enforce ownership of items in the handler ([`extension.py:86-89`](extension.py)).
* Pitfall: `$remind 2026-10-20 08:00 x` reads only the date (midnight) and keeps `08:00 x` as the
  text. Write ISO times with `T`.

See also: [Capabilities](../../docs/core/EXTENSIONS.md#capabilities) (ToolContext and `schedule`) and
[Declaring commands](../../docs/core/EXTENSIONS.md#declaring-commands) in the module guide,
[Permissions](../../docs/core/ARCHITECTURE.md#permissions) in the architecture,
[`basic`](../basic/README.md), [`system`](../system/README.md).
