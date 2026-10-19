# `system`: host status

A built-in module with one capability: a snapshot of the machine the bot runs on (time, CPU load and
temperature, memory, storage, uptime). People type `$status`; models can call it when asked how the
server is doing. Study it as the smallest capability that wraps a third-party library and does blocking
work without stalling the bot.

## What it adds

| Kind | Name | Tier | Access | `$command` | Notes |
| --- | --- | --- | --- | --- | --- |
| Capability | `system.status` | read | member | `$status` | No parameters |

No interfaces, engines, personas, profiles, defaults or declared settings.

## Setup

Nothing to configure: no `config.example.yml` and no `.env` variables. It needs `psutil` (listed in
`requirements.txt`). To turn it off:

```yaml
modules:
  exclude: [extensions/system]
```

## Using it

```
$status
Time: 2026-10-19T14:03:00+08:00
CPU: 3.5% of 4 cores, 48.2 °C
Memory: 1.9 GiB / 7.9 GiB (24%)
Storage: 21.4 GiB / 58.0 GiB (37%)
Uptime: 3d 4h 12m
```

* The temperature is left out where `psutil` has no sensors (Windows, macOS) or none reports a value.
* Storage is the root of the home directory's drive: always `/` on Linux and macOS, the home drive
  (such as `C:\`) on Windows.
* It is a read with `member` access, so anyone admitted to a conversation can run it. To hide host
  details somewhere, take it out of that conversation's `use` list, as the docs' example does for WeChat:

```yaml
permissions:
  "wechat:*": {use: ["*", "!system.*"]}
```

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `temperature`, `collect` (the `psutil` calls), the `status` handler, `setup` and `EXTENSION` |

`collect` gathers everything into a plain dict; `status` formats it into lines for the summary and
returns the dict as `data` (raw byte counts, percentages, `uptime_s`, `temperature_c`), so an engine
or a `$command` caller can use the numbers without parsing text.

`psutil.cpu_percent(interval=0.2)` sleeps for 0.2 seconds while it measures. The handler therefore
runs `collect` with `asyncio.to_thread`, keeping the event loop (and every other conversation) free.
`temperature` tries the usual sensor names first (`cpu_thermal` on a Raspberry Pi, `coretemp`,
`k10temp`, `acpitz`), then any other, and returns `None` instead of failing.

## Data it keeps

Nothing.

## Tests

`tests/test_core_ported.py`, class `SystemTests`: `$status` inside a real core answers with the CPU,
memory, storage and uptime lines.

```
venv/bin/python -m unittest tests.test_core_ported.SystemTests
```

## Building something similar

* The whole declaration is one call ([`extension.py:56-58`](extension.py)): a capability without
  parameters needs only a name, a description, the handler, a tier and a command.
* Move blocking calls off the event loop: `await asyncio.to_thread(collect)`
  ([`extension.py:44`](extension.py)). Handlers are `async` and share one loop with the whole bot.
* Degrade instead of failing when a platform lacks a feature ([`extension.py:14-26`](extension.py)).
* Return both a short summary for people and models and the raw values in `data`
  ([`extension.py:52`](extension.py)).
* Pitfall: "read" says nothing about who may see the result. If the output is sensitive, set
  `access="admin"` or `"creator"` on the capability, or mark it `private` for the owner's private data.

See also: [Capabilities](../../docs/core/EXTENSIONS.md#capabilities) in the module guide,
[Permissions](../../docs/core/ARCHITECTURE.md#permissions) in the architecture,
[`basic`](../basic/README.md), [`reminders`](../reminders/README.md).
