# Writing extensions

An extension is a directory with an `extension.py`. It can add **interfaces** (platform
connections), **engines** (turn loops), **capabilities** (actions) and default **personas**, in any
combination. The core treats your code as a black box and you treat the core as one: the only shared
code is the [`contracts`](../../contracts) package, which is the complete public API. This guide
describes everything the core does with it; you should not need to read core code.
[ARCHITECTURE.md](ARCHITECTURE.md) explains the internals for core maintainers.

## Ground rules

* Import `contracts`, the standard library, third-party packages and your own directory. Import
  nothing else from this repository. To reuse older code from this repository (for example a
  platform transport), move or copy it into your extension directory and own it there.
* Inside your extension anything goes: desktop automation, private databases, subprocesses, your own
  model clients. The core does not inspect how you do your job.
* Two kinds of traffic must go through the core, because ordering, deduplication, policy, approvals
  and history live there:
  1. **Conversation traffic.** Messages in through `InterfacePort.submit`; replies out through the
     core (`Session.reply`, `send_file`, `progress`). An interface never answers a message on its own.
  2. **Tool use by engines.** Engines call capabilities only through `Session.tools.call`.

## Concepts

| Term | Meaning |
| --- | --- |
| Address | `interface:conversation`, e.g. `wechat:room@chatroom`. The conversation part is yours to define and may contain `:`. |
| Owner | A sender listed under `owners` in `core.yml` for that interface, e.g. `owners: {discord: ["1234"]}`. Owners approve writes, run owner-only capabilities, and their `$commands` bypass admission. |
| Control route | `control: discord:dm-1234` in `core.yml`. Approval requests, failure details, events without a route, routine replies without a route, and hidden progress go there. |
| Route | Gateway data `pattern → {target, kinds}`: replies (of the listed `kinds`, or all) for conversations matching `pattern` go to `target` instead. `kinds` may include `event` to send interface events somewhere other than the control route. Edited with `core.set_route`. |
| Admission rule | Gateway data deciding which person messages become requests (below). Edited with `core.set_rule`. |
| Assignment | Manager data deciding which engine and personas handle a conversation, what they may use, and their budget (below). Edited with `core.set_assignment`. |
| Pattern | `*`, `interface:*`, or an exact address. The most specific match wins. |
| Run | One handled request (or a resumed one), durable across restarts. |
| Lane | Where a new message goes. A run *holds* its lane only while it is thinking or waiting for an answer; new messages then go to its inbox (or answer its question). While a run waits on a capability or a file delivery (a video conversion, a ten-minute upload) or for approval, it does not hold the lane, and new messages start their own run in parallel; engines see such work through `session.activity()`. Default lane = conversation; with `lane: sender`, each sender in a group conversation has their own lane, so only the person asked can answer a question. |
| Forward | Gateway data `address → target`: the owner's ordinary messages in `address` are sent on to `target` (only if `target` is admitted). Edited with `core.set_forward`. |

### Admission rules

```yaml
admission:
  "*":           {enabled: true, require_mention_in_groups: true}
  "wechat:*":    {enabled: false}                       # nothing from WeChat unless allowed below
  "wechat:room@chatroom": {enabled: true, commands: owners, max_age_s: 900}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | true | Admit person messages at all. |
| `allow_senders` | null (all) | List of sender IDs; owners always pass. |
| `require_mention_in_groups` | true | Non-direct messages need `mentions_me=True`. |
| `owner_only` | false | Only owners. |
| `max_age_s` | none | Reject messages whose `received_at` is older than this (bounds catch-up after downtime). |
| `commands` | `all` | `owners`: only owners' `$…` messages are commands; others are ordinary text (so "$AAPL" is not a command). |

Whenever a rule is created or changed, messages received **before** the change are not admitted
(`before_rule_change`). Enabling a conversation therefore never answers its backlog, even when an
interface catches up after downtime. Re-applying an identical rule keeps its original time.
A rule that stops admitting a conversation also cancels that conversation's open runs and expires
their pending approvals. Routine and system requests are always admitted, but run output (including
routine output routed to a conversation) is only delivered to admitted conversations.

### Assignments

```yaml
assignments:
  "*":
    engine: assistant
    personas: [assistant]          # or persona: assistant
    allow: ["*"]                   # capability patterns this conversation may use
    deny: []
    preapprove: ["basic.remember"] # writes that run without asking the owner
    budget: {model_calls: 24, tool_calls: 24, seconds: 900}
    trusted: false                 # routines only: may use owner-only capabilities
    lane: conversation             # or sender
    enabled: true                  # false: record messages, start no runs
    options: {instructions: "Keep replies brief."}   # passed to the engine as session.options
```

Assignments in `core.yml` are fixed at startup. `core.set_assignment(pattern, assignment)` adds or
replaces a runtime assignment (same keys) that takes priority over a configured one for the same
pattern; requests received before that moment do not start runs under it. `core.delete_assignment`
removes runtime ones; `enabled: false` turns a conversation's engine off and cancels its open runs
(pending approvals expire; an approval decided afterwards reports it expired).

### Personas and model profiles

```yaml
providers:
  deepseek: {kind: openai_compatible, base_url: "https://api.deepseek.com", api_key_env: DEEPSEEK_API_KEY,
             schema_mode: json_object, effort_param: true}
profiles:
  default: [{provider: deepseek, model: deepseek-flash}, {provider: openai, model: gpt-6-luna}]
personas:
  whale: {display_name: 本鲸, profile: default, prompt: "…"}
```

A profile is an ordered list of candidates; the next is tried when one fails. An extension may ship
default personas in `Contributions.personas`; a configured persona with the same name wins.

## Layout and declaration

```
my_extension/
  extension.py        # defines EXTENSION
  anything_else.py    # import with relative imports: from .anything_else import X
  config.example.yml  # optional: documented settings to copy
  config.yml          # optional, git-ignored: this extension's local settings
```

```python
from contracts import Contributions, Extension, Persona

def setup(context) -> Contributions:
    # context.name, context.config (local config.yml, overridden by core.yml), context.data_dir (private dir),
    # context.log(event, **fields)
    return Contributions(interfaces=[...], engines=[...], capabilities=[...],
                         personas=[Persona("whale", "本鲸", "…", profile="default")])

EXTENSION = Extension("my_extension", setup, contracts="1", version="1", description="...")
```

* `name`: lowercase letters, digits and `_`; unique across loaded extensions.
* `setup` may be `async`. It runs once at startup; start network loops in `Interface.start`, not here.
  Objects created in `setup` may be shared by your interface, engines and capabilities.
* `contracts="1"` is the contracts major version you target; a mismatch refuses to load.
* **Drop-in:** put the folder into `plugins/` and restart; every folder there with an `extension.py`
  loads without touching `core.yml` (`plugin_dirs` lists the drop-in directories; default `plugins/`).
  Folders in `plugins/` and every `config.yml` are git-ignored; an extension that is its own repository
  should also ship a `.gitignore` listing `config.yml`. [`plugins/whale_chan`](../../plugins/whale_chan)
  is a complete example: a second Discord bot with its own engine and persona.
* Or list a folder from any repository in `core.yml`:

```yaml
extensions:
  - ../MusicManager/bot_extension
extension_config:
  my_extension: {some_option: 1}      # merged over the folder's config.yml into context.config
```

* **Settings:** `context.config` is the folder's own `config.yml`, with `extension_config.<name>` from
  core.yml merged over it.
* **Secrets:** prefer `.env` and name the variable in the config (for example
  `token_env: DISCORD_BOT_TOKEN_WHALE_CHAN`). Putting a secret directly in the folder's `config.yml`
  (`token: …`) is supported for people who would rather not edit the environment; then
  `chmod 600 config.yml`, and never share or copy the folder together with its `config.yml`.
* **Defaults that make a drop-in work on its own:** `Contributions` may also carry model `profiles`
  (name → ordered candidates, as in core.yml) and `assignments` / `admission` for the extension's own
  interfaces only (`<interface>:*` or `<interface>:<conversation>`; anything else refuses to load).
  core.yml and runtime edits always win.

`python -m core --config core.yml --check` loads and validates every extension and lists what each
contributes, without starting anything.

## Interfaces

```python
from contracts import Address, Delivery, Display, Interface, Operation, Reply, Request, Sender

class MyInterface(Interface):
    name = "myplatform"                 # becomes Address.interface; unique; not "routine"
    display = Display(markdown=False, quotes=True, files=True, progress=False,
                      max_upload_mb=100, max_text_chars=2000)
    operations = (Operation("status", "Show account status", command="mystatus"),)

    async def start(self, port): ...    # store port; launch your loops as tasks; return promptly
    async def stop(self): ...           # cancel loops; do not log out the account
    async def deliver(self, reply: Reply) -> Delivery: ...
    async def operate(self, operation, arguments, context): ...   # returns ToolResult
```

### The port

| Method | Use |
| --- | --- |
| `await submit(request) -> SubmitResult` | Hand a message to the core (below). |
| `await notify(text, origin=address, code=..., data=..., files=...)` | Tell the owner something: an incoming message from a conversation the owner follows, a login change. Delivered as a `Reply(kind="event")` to the route for `origin` whose `kinds` include `event`, else the control route. If the owner replies to that delivered event on their platform (`Request.reply_to` = the event's message ID), the core relays the reply text to `origin` as an owner message and confirms with a `relayed` notice. Which conversations to notify about is your extension's decision and configuration. |
| `await observe(address, text, direction="in"/"out", sender=..., kind=..., message_id=..., at=..., reply_to=..., quoted_text=..., files=...)` | Put activity into conversation history without starting anything: a reply someone typed on the phone, group chatter not addressed to the bot (for example after `submit` returned `not_mentioned`), an image sent before the question about it (`files`: local copies engines may show to models), or a delivery confirmed late. Idempotent per `(address, direction, message_id)`. |
| `await cancel(address, reason) -> int` | Cancel the conversation's open runs (all lanes) because platform state changed (a manual reply made pending work stale). Pending approvals of those runs expire; their later output is not sent. Safe to call from inside `deliver`; the cancellation takes effect for everything after it. |
| `await cancel_all(reason) -> int` | The same for every conversation of your interface (account switched, signed out). |
| `check(reply) -> str \| None` | Re-check a reply right before you commit it, after waiting for spacing or a lock: `None` means send; otherwise `run_closed`, `conversation_disabled` or `not_admitted`, and you must not send (return `Delivery(ok=False, detail=reason)`). |
| `policy(address)` | Read-only `{"admission": rule, "assignment": assignment or None}` for one of your conversations, for status displays. |
| `set_owners(ids, control=None)` | Report the platform's own owner, e.g. a bot application's owner. Used only when `core.yml` lists no owners for your interface; `control` (one of your addresses, such as the owner's DM) is used only when no control route is configured. This is how a fresh Discord setup routes approvals to the bot owner's DM without configuration. |
| `set_ready(ready, detail="")` | Report whether you can send. The first call with `False`, and every change, sends an `interface_ready` / `interface_not_ready` event (with `data["detail"]`) to the owner. While not ready, deliveries to you are refused (`Delivery(ok=False, detail="interface_not_ready")`, not uncertain). Interfaces start ready. |
| `bottle` | Your private persistent store (below). |
| `commands()` | The command catalog (`Command` declarations: built-ins, capability and operation commands) for a native command menu or your own help. Translate a menu selection into its `$` text with `command.render(values)` and submit that as the request's text. |
| `log(event, **fields)` | Audit log entry (no message text, please). |

All port methods act only on your own interface's addresses; anything else raises `ValueError`.

### Submitting requests

| Field | Meaning |
| --- | --- |
| `id` | Unique within your interface and stable across restarts (use the platform's message ID). The gateway remembers IDs for 30 days and drops repeats, so resubmitting is safe. |
| `address` | `Address(self.name, conversation_id)`. If the platform account can change, include the account identity in `conversation_id` (for example `<account>/<chat>`) so policy for one account never applies to another. |
| `sender` | `Sender(id, name)`. `id` must be stable; owners are configured by these IDs. |
| `text` | Message text. For media without text, a short label such as `[image]`. |
| `kind` | `text`, `image`, `video`, `voice`, `file`, `sticker` or `other`. Engines can tell they cannot see media. |
| `files` | `FileRef`s to local files you downloaded (optional). |
| `received_at` | Epoch seconds the platform received it. Admission compares it with rule changes and `max_age_s`. |
| `is_direct` | `True` for one-to-one conversations. |
| `mentions_me` | For groups: `True` only if the message explicitly addresses this account. What counts (a mention entity, a quote-reply to the bot) is your interface's decision; prefer platform metadata over text matching. `None` if unknown. |
| `reply_to`, `quoted_text` | ID and text of the message this one quotes or replies to, if any. |
| `display` | What this conversation can show; usually `self.display`. |
| `extra` | Opaque string facts for your own use; the core stores but does not interpret them. |

`origin` stays `"person"`; only the core creates routine requests.

`submit` returns a `SubmitResult`: truthy with `reason` `accepted`, `command` or `relayed` (an owner
message sent on), or falsy with `duplicate` or the admission decision (`disabled`, `not_mentioned`,
`sender_not_allowed`, `owner_only`, `before_rule_change`, `too_old`). If it raises, nothing was
accepted; submit it again later. **Advance a read cursor only
after `submit` returns.** Submit messages of one conversation in order; different conversations may
be submitted concurrently. Messages starting with `$` and a letter are commands (subject to the
`commands` rule): `$approve <id>`, `$deny <id>`, `$runs`, `$cancel [run-id|address]`, `$tools`,
`$memory` (anyone: read, forget or clear what personas keep about them in this conversation),
`$admin` (admins: status, list, lock/unlock, own view on/off; the creator also adds and removes admins),
`$help [command]`, `$call <capability> <json arguments>` (owners only: run any capability directly, for
example `$call core.set_rule {"pattern": "wechat:x", "rule": {"enabled": true}}`; counts as approval), plus
capability and operation commands (`$help` lists them). The core only understands this `$` text;
platforms with their own command menus (Discord slash commands) translate into it.

### Interface settings

Per-conversation settings that only your interface uses (send spacing, notification preferences,
nickname aliases) belong in your bottle, edited through your own operations. Settings that decide
what the core does (admission, engine, persona, instructions) belong in rules and assignments; read
them with `port.policy(address)` when you need them (for example in a status operation).

### Your bottle

`port.bottle` (`get`, `set`, `delete`, `keys`, JSON values) is committed to disk when `set` returns.
Keep transport state there: read positions, native ID mappings, login state. It is separate from the
bot's knowledge, and no other interface can read it. Keep it small (thousands of keys, not millions);
prune your own old entries.

A restart pattern that works: keep the last submitted position per conversation. On start, read
from it and submit anything newer (duplicates are dropped; rules bound age and backlog). On the very
first start for a conversation, record the current position without submitting history.

### Delivering replies

`deliver(reply)` is called for each outbound item addressed to you. The core may call it
concurrently for different conversations and for different runs of one conversation; serialize
inside your interface if your platform needs it (desktop control, per-chat send spacing). There is
no core timeout and time spent here does not count against run budgets, so waiting for spacing or a
ten-minute upload is fine. Before calling you for a run's output, the core re-checks that the run is
still open, its assignment is enabled and the conversation (and any routed target) is still admitted.
Cancelling a run does not interrupt a `deliver` already in progress: if you wait before committing
(spacing, a desktop lock), call `port.check(reply)` immediately before the irreversible step.

`reply.actions` are buttons (`Action(label, command, style)`): a press runs `command` (`$approve
ea947b`) as the person who pressed it, so submit it as a request from them. Without buttons, show the
commands as text. `reply.private` asks to show the reply only to the person it answers (Discord:
ephemeral); otherwise send it normally.

Outbound replies are not queued durably. If the process stops during `deliver`, the core does not
call `deliver` again for that reply after a restart (engines prefer at-most-once). `reply.id` is
unique and stable, so you can record in your bottle what you committed. If a send you reported as
uncertain is confirmed later, record it with `observe(..., direction="out", message_id=...,
reply_to=reply.in_reply_to)`. For multi-part output (split text, caption plus file), return the first
platform message ID, quote only the first part, and report `uncertain=True` if some parts may have
been sent and others failed.

Return a `Delivery`:

* `ok=True, message_id=...` once you have evidence the platform has it.
* `ok=True, detail="suppressed"` if you deliberately do not show it (for example a notice you do not
  want in a group). You may also merge items nobody explicitly asked for (progress routed from another
  conversation, failure details, relay confirmations) into one edited message and return its ID; the
  included Discord interface keeps one rolling code block per conversation for up to an hour.
* `ok=False` if you know nothing was sent; `ok=False, uncertain=True` if it may have been sent.
  Raising counts as uncertain. The core never retries; the engine sees your `Delivery`.

| `reply.kind` | Render |
| --- | --- |
| `text` | `reply.text`. If `reply.quote` is set (only when `display.quotes` is true), quote that request ID. Split long text if needed. If `reply.speaker` is set (engines with several personas), name the speaker, with `reply.label` if any (Discord: `**Luna · Work note**` above the text). |
| `file` | Send `reply.file.path` (named `reply.file.name`), caption `reply.text`. The core already checked `display.files` and `max_upload_mb`. |
| `progress` | A transient status line. You get it only if your `display.progress` is true or you are the control route. |
| `notice` | Phrase `reply.code` in your platform's style (table below). |
| `event` | An interface event for the owner (you receive these if you host the control route or an event route). Show `reply.text`; `reply.data["origin_address"]` and `reply.data["interface"]` say where it came from. Return the `message_id` so owner replies can be relayed. |

`reply.source` says who produced it: `run`, `command`, `owner` (a relay or owner send) or `core`.
`reply.in_reply_to` is the triggering request ID; `reply.run_id` the run. Replies routed from another
conversation carry `data["origin_address"]`.

| Notice `code` | When | `data` |
| --- | --- | --- |
| `run_failed` | The request could not be finished. Show a short neutral message in the conversation's language (or suppress it). | `error_code` |
| `run_failed_detail` | Details for the owner (control route). `text` is English. | `error_code`, `address` |
| `approval_request` | An action needs the owner's approval (control route). `text` is an English summary. | `approval_id`, `capability`, `address`, `arguments` (JSON) |
| `waiting_approval` | A `$command` is waiting for approval. | `approval_id` |
| `relayed` | An owner reply to an event was sent on. | `address`, `ok`, `detail` |
| `unknown_command` | Unknown `$command`. | `command` |
| `not_permitted` | The command is not available here. | |

### Declaring commands

A plain `command="remind"` is enough: it becomes a `Command` with one option for `command_argument`.
Declare a `Command` to describe the command better for help and native menus:

```python
from contracts import Command, Option

Command("remind", "Set a one-time reminder here",
        (Option("when", "10m, 2h30m, 18:30 or a date", required=True),
         Option("text", "What to remind you of", required=True)),
        template="{when} {text}", examples=("10m drink water",))
```

* `options`: typed arguments (`string`, `integer`, `number`, `boolean`, `attachment`), with
  `required` and `choices`. A boolean with `flag="--fit"` renders as that flag when true.
* `template` turns option values back into the text after `$name` (`{option}` placeholders; `[...]`
  sections are dropped when an option inside has no value), so `/clip url:… start:1:05` becomes
  `$clip -u … -s 1:05`. Without one, values are joined with spaces. The text is what reaches your
  handler in `command_argument`, whichever way the command was typed.
* `examples`, `admin_help` (extra help for admins and the creator), `access` (`member`, `admin` or
  `creator`: the lowest level that may run it; `owner_only` capabilities are `creator`), and
  `message_action` (a label for running the command on an existing message where platforms support it).

### Operations

`operate(operation, arguments, context)` receives the same `ToolContext` as a capability: who asked
(`address`, `sender`), `run_id`, a `workspace` and `progress`. `operations` are platform-specific actions (login QR, account status, list chats, playing music). Each becomes
a capability named `<interface name>.<operation>`, owner-only unless `owner_only=False`; a public
operation must only act on the calling conversation (`context.address`); if your extension also declares a
capability with that exact name, loading fails. `risk` works as for capabilities (`write` needs
approval unless pre-approved); `command`/`command_argument` add a `$command`; `timeout_s` defaults to
120. `operate` returns a `ToolResult` (attach a QR image in `files`). For results that arrive later
(login confirmed on the phone), use `port.notify` and `port.set_ready`.

## Capabilities

```python
from contracts import Capability, Risk, ToolResult

async def find_tracks(arguments, context) -> ToolResult:
    ...
    return ToolResult.ok("Found 3 tracks", data={"paths": [...]})

Capability("music.find_tracks", "Search the library by artist or title.", find_tracks,
           parameters={"artist": {"type": "string"}, "title": {"type": "string"}},
           required=(), risk=Risk.READ)
```

| Field | Meaning |
| --- | --- |
| `name` | `<extension name>.<action>`; the namespace must be your extension's name. |
| `description`, `parameters`, `required` | Offered to models; `parameters` are JSON-schema properties. Arguments are validated before your handler runs; extra properties are rejected. |
| `risk` | `read`: observes only; may run again. `prepare`: creates local artifacts (downloads, renders); runs once per run. `write`: changes something outside (send, edit, delete, schedule); needs owner approval unless pre-approved; runs once per run. Declare honestly; the policy depends on it. |
| `owner_only` | Only owners (or trusted routines) may use it. |
| `needs` | `Display` features the conversation must have, e.g. `{"files"}`. Whether incoming attachments exist is not a feature; capabilities that read them check the request's `files` themselves. |
| `internal` | Callable by engines but not shown to models. |
| `command`, `command_argument` | Also available as `$command <text>`, with the text in that parameter. An owner's `$command` counts as approval of that exact call. `command` is a name, or a `Command` declaration (below). |
| `timeout_s` | Hard limit for one call (default 600). |

**Access levels.** `access="admin"` or `"creator"` (or `owner_only=True`) limits who may call a
capability at all; `context.access` (`member`, `admin`, `creator`) is the caller's effective level, so
one capability can also behave differently by level, for example listing only the caller's own items
for members and every conversation's items for admins. Describe the admin behaviour in the command's
`admin_help`; `$help` shows it to admins only.

The handler receives validated `arguments` and a `ToolContext`: `address`, `sender`, `origin`,
`run_id`; `display` (what the calling conversation can show, e.g. `max_upload_mb` for fitting files);
`files` (files people attached to the run's requests); `storage` (your extension's private store, shared by your capabilities, committed on
`set`); `models` (model calls through the core, counted in the run's budget); `workspace` (a
directory for this run's output files); `await progress(text)`; `await schedule(at, text)`
(a one-time `reminder` notice in the calling conversation at Unix time `at`; returns its ID; raises
`ValueError` for past times, more than a year ahead or more than 50 pending), `scheduled()` (the
conversation's pending items: `id`, `at`, `text`, `sender_id`, `sender_name`) and
`await unschedule(id)`. Interfaces render `reminder` notices; `data` has `sender_id`, `sender_name`
and `at` (to mark late delivery).

Return a `ToolResult`; do not raise for expected failures. `ToolResult.ok(summary, files=…,
sources=…, data=…)`, `ToolResult.fail(summary, error_code, retryable=False)` (`retryable=True` only for
transient read failures), `ToolResult.ask(question)` when you need information from the person.
Summaries are facts for the engine; keep them short. Exceptions become `failed` results named after
the exception type.

## Engines

```python
from contracts import Engine, EngineSpec, ModelRequest, RunYield

class MyEngine(Engine):
    spec = EngineSpec("my_engine", "What it does", personas="one")   # or "many"

    async def run(self, session) -> RunYield:
        ...
        return RunYield.DONE
```

`session` gives you:

| Handle | Use |
| --- | --- |
| `trigger` | The request that started the run (`display`, `is_direct`, `sender`, `kind`, `reply_to`, …). |
| `events` | The run's durable event log: `input` (`data["request"]` is the request as a dict), `tool_result` (`call_key`, `name`, `arguments`, `result` of an approved action; `call_key` is `"call:" + call_id`), `approval_denied` (`call_key`, `name`, `arguments`), `resumed`. Indexes increase. |
| `state`, `await save(state)` | Your JSON state; persisted on `save`. |
| `resumed` | `True` when the core restarted during the run. |
| `options` | The assignment's `options` mapping (per-conversation settings). |
| `owner` | Whether the trigger's sender is an owner of its interface (never for routines). |
| `access` | The trigger's sender's effective level: `member`, `admin` or `creator` (see below). |
| `personas`, `memory(name)` | Personas of this conversation; each persona's private persistent store. |
| `diary(name)` | That persona's notes about the trigger's sender in this conversation (see below). |
| `history.recent(limit)` | This conversation's history, oldest first: every admitted request and `$command`, messages interfaces `observe`, relayed owner messages, and successfully delivered replies and files (`direction="out"`, `reply_to` = the request they answered). Non-admitted messages, notices, progress and failed or uncertain deliveries are not recorded. Items keep their attachments in `files` (request files, observed files, delivered files). Filter by `sender_id` yourself if needed. |
| `inbox()` | Requests for this run's lane that arrived while this run was thinking; each is returned once. Messages arriving while the run waits on a capability or a file delivery start their own run instead. |
| `activity()` | Other open runs in this conversation (`Activity`: `run_id`, `status` `working`/`running`/`waiting_input`/`waiting_approval`, `sender_id`, `sender_name`, `request`, `doing`, `since`), so an engine can say "still converting" instead of starting the same work again. |
| `models.generate(ModelRequest)` | Model calls (default profile: the first persona's). With `output_schema`, `result.data` is parsed and validated JSON. `images` (up to 6 local JPEG/PNG/GIF/WebP files, for example the request's attachments) are shown with the last user message; candidates whose provider has `vision: false` are tried last and told images were attached. Raises `ModelError` (`unavailable`, `invalid_output`, `budget_exceeded`, `not_configured`). |
| `tools.list()`, `await tools.call(name, args, call_id)` | Capabilities allowed here, and calling them under policy. Never raises for policy; check `result.status` (`success`, `failed`, `needs_input`, `needs_approval`). |
| `await reply(text, quote=None, persona=None, label="")`, `await send_file(file)`, `await progress(text)` | Output; returns the interface's `Delivery` (or a refusal such as `run_closed`, `conversation_disabled`, `too_large`). `persona` (one of the session's) makes the persona's display name the reply's `speaker` and the sender name in history; `label` qualifies it (for example a work note, recorded with kind `note`). |

Return one of:

* `RunYield.DONE`: finished. Unread inbox messages start a new run.
* `RunYield.WAIT_INPUT`: you asked a question; the next message in this lane resumes this run as an
  `input` event. Use `lane: sender` in groups so only the person you asked can answer.
* `RunYield.WAIT_APPROVAL`: a tool call returned `needs_approval`. The owner's decision resumes the
  run with a `tool_result` or `approval_denied` event.

Exceptions fail the run: the conversation receives a `run_failed` notice and the owner the detail.

### Writing a resumable engine

`run` is called again after a restart, after an answer and after an approval. Make it replay safely:

* Keep everything you need in `state` and `save` after each meaningful step.
* Give every tool call a deterministic `call_id` and save it **before** calling. Calling again with
  the same `call_id` returns the stored result instead of running again; an interrupted prepare or
  write returns `uncertain_previous_attempt` rather than repeating. Identical prepares/writes without
  a `call_id` also run only once per run.
* Track which events you have processed (by index).
* Prefer at-most-once for messages: save that you are about to speak, then speak.

### The built-in `assistant` engine

[`extensions/assistant`](../../extensions/assistant/engine.py) is a reference engine you may assign
to any conversation:

* One persona. The system prompt is the persona prompt, its display name, `options.instructions`,
  then a turn contract. Each step the model returns `{message, status, actions}` (`continue` with
  actions, or `complete` / `needs_input` / `blocked` without), validated with up to two corrections.
* It speaks `message` before running actions, runs them in order with saved `call_id`s, stops a batch
  at the first failure, and sends files from successful results itself. It tells the model never to
  announce an outcome before the result arrives.
* `needs_approval` → `WAIT_APPROVAL`; `needs_input` from the model → `WAIT_INPUT`. New inbox messages
  are added before each step. Other open runs in the conversation (`activity()`) are listed in the
  system prompt so the model does not repeat work in progress.
* In non-direct conversations it prefixes speakers' names and quotes the latest request (where
  `display.quotes`). `options.history_scope: sender` limits history to that sender's messages and
  the replies to them.
* At its step limit it asks the model for a short closing message without actions; it never emits
  fixed text of its own. Model failures fail the run (the interface phrases `run_failed`).
* A failed or refused text delivery does not stop it; file deliveries are reported to the model as
  delivered or not. It does not regenerate a reply when newer input arrives; it reads the inbox before
  the next step instead. Engines needing different direct/group behaviour should be their own engine.
* Assignment options: `instructions` (added to the system prompt), `owner_instructions` (added only
  when an owner is speaking), `max_steps`, `history_limit`, `history_image_minutes`, `history_scope: sender`,
  `quote: latest` (default; quote the newest message of the run) or `quote: trigger` (keep quoting
  the original request).
* Extension options (`extension_config.assistant`): `max_steps` (8), `history_limit` (16), `effort`
  (`low`).
* Images attached to the run's messages, found by its tools, or sent by the person in history shortly
  before the request (`history_image_minutes`, default 30; WeChat sends a picture and its question as
  separate messages) are shown to the model on every turn (latest 6). History lines carry the time,
  the speaker, what they replied to and their attachments.
* Diary: the system prompt includes the persona's notes about the sender (matching ones first), and a
  turn may add `diary: [{key, kind, note, importance}]` (at most 2; importance 3+ is kept). Turn it
  off with the assignment option `diary: false`.

### Diaries

`session.diary(persona)` returns a `Diary` about the person who sent the trigger, in this conversation
only; a DM disclosure never shows up in a server. `recall(text, limit)` returns `DiaryNote`s, those
matching words of `text` first. `write(key, kind, note, importance)` creates or replaces the note
under `key` (`kind` is one of `DIARY_KINDS`: fact, episode, reflection). The core refuses
(`ValueError`) invalid keys or values, notes over 400 characters, notes that copy the message instead
of paraphrasing it, and more than 4 changed notes per run; it keeps at most 200 notes per persona and
person, dropping the least important oldest. Routine runs have no person: `available` is false,
`recall` is empty and `write` raises. People manage their notes with `$memory`, so engines need no
commands of their own for this.

### The built-in `room` engine

[`extensions/room`](../../extensions/room/engine.py) is the shared room: several personas own one
request together on a work board. It also contributes the
four default characters `luna`, `gemini`, `claude` and `deepseek` (profiles of the same names).

* Who answers: personas named in the message (name, display name or `aliases`), everyone for
  "大家", "everyone" and similar, otherwise one persona in rotation per conversation.
* Each turn returns `{status, message, work_note, actions, jobs, diary}`. The message is spoken
  (`speaker` set) before tools run; `work_note` is a short public explanation sent with label
  `Work note` / `工作说明`. `jobs` hand teammates focused work; jobs without `after` run in
  parallel, one job per persona at a time, and a blocked prerequisite blocks its dependents.
* Every persona sees the conversation history (her own messages as hers), the board, recent room
  messages, shared tool results, the images attached to the run's messages, and only her own diary.
* Writes wait for the owner's approval while other jobs continue; `needs_input` waits for the
  person's answer. Both keep the board in the run, so they survive restarts. New messages during a
  run answer waiting questions or open new jobs.
* Guards: a successful write is never repeated in a run, a failed action is
  retried once only if retryable, invalid turns get one correction, and a step limit (`max_steps`,
  24) bounds the board. A failing persona reports it in the person's language; the others continue.
* Extension options (`extension_config.room`): `max_steps`, `history_limit` (16), `max_tokens`
  (1500), `aliases` (`{claude: [Haiku]}`). Assignment options: `instructions`, `owner_instructions`,
  `max_steps`, `history_limit`, `history_image_minutes`, `diary: false`.
* Budget: a busy room makes many model calls; give its assignment a larger `budget`.

### The included Discord interface

[`extensions/discord`](../../extensions/discord/interface.py) renders replies with Markdown, speaker
labels, a rolling status block for unrequested notices, and math: inline `$…$` becomes Unicode and
display `$$…$$` is sent as PNG images between the text parts. Its public operations (usable by
anyone, acting only on the calling conversation) are `discord.music` (`$music`), `music_status`,
`music_control`, `queue_music` (one player per server, in the caller's voice channel), `time`
(`$time`), `clock_start` (`$start`) and `clock_stop` (`$stop`); `status` and `invite` are owner-only.
Options: `talk_prefixes` (for example `["$agent", "$chat"]`: a message starting with one is addressed
to the bot like a mention, without the prefix), `music_dir`, `music_list`, `ffmpeg`, `clock_interval_s` (1), `clock_max` (10), `math` (true),
`math_width_px`, `math_dpi`, `math_font_size_pt`, `math_min_height_px`, `math_max_images` (10).

## Core capabilities

All owner-only; the writes need approval unless pre-approved (an owner's `$command` counts as
approval).

| Capability | Parameters |
| --- | --- |
| `core.send_message` (write) | `address`, `text`. An explicit owner send: not subject to admission. Returns `data["message_id"]`; fails with `delivery_failed` or `delivery_uncertain`. `owner_only` is judged by the run's trigger sender. |
| `core.list_rules` / `core.set_rule` | `pattern`, `rule` (object or null to delete) |
| `core.list_routes` / `core.set_route` | `pattern`, `route` (`{target, kinds}` or null) |
| `core.list_routines` / `core.set_routine` / `core.delete_routine` | `name`, `text`, `every_minutes` or `daily_at` (`HH:MM` local), `enabled` |
| `core.list_assignments` / `core.set_assignment` / `core.delete_assignment` | `pattern`, `assignment` |
| `core.cancel_runs` | `address` |
| `core.list_forwards` / `core.set_forward` | `address`, `target` (exact addresses; null removes) |
| `<interface>.<operation>` | as declared |

## Retention

Request IDs for deduplication and event receipts for owner relays are kept for 30 days. Run
workspaces are deleted at startup 7 days after their run ended; copy files elsewhere if they must
live longer. Conversation history, runs and approvals are kept indefinitely.

## Routine example

A reminder that reaches a conversation: the owner creates `core.set_routine(name="medicine",
daily_at="08:00", text="Remind everyone to take their medicine.")` and
`core.set_route(pattern="routine:medicine", route={"target": "wechat:family@chatroom"})`. The run happens
in `routine:medicine` under the `routine:medicine` (or `routine:*`) assignment; its replies go to the
target only while that conversation is admitted. Non-owners cannot create routines.

## Testing

There is no test package in `contracts`; copy the pattern. Load your extension with a real core in a temporary directory: a small `core.yml` listing your
extension plus a test interface whose `deliver` records replies and which calls `port.submit`
directly. See [`tests/core_support.py`](../../tests/core_support.py) and
[`tests/core_fixtures`](../../tests/core_fixtures) for working examples, and
[`tests/test_core_boundaries.py`](../../tests/test_core_boundaries.py) for the import rules.
