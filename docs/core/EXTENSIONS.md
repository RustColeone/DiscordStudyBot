# Writing modules

A module is a directory with an `extension.py` (the contract still calls the declaration an
`Extension`). Built-in modules live in [`extensions/`](../../extensions) and are maintained and tested
with the core; external ones are dropped into [`plugins/`](../../plugins) or live elsewhere. Every
module loads unless `core.yml` excludes it. A module can add **interfaces** (platform
connections), **engines** (turn loops), **capabilities** (actions), default **personas** and **watchers**
(learning from what personas say), in any combination. The core treats your code as a black box and you treat the core as one: the only shared
code is the [`contracts`](../../contracts) package, which is the complete public API. This guide
describes everything the core does with it; you should not need to read core code.
[ARCHITECTURE.md](ARCHITECTURE.md) explains the internals for core maintainers.

## Learning from the included modules

Every module folder has a README: what the module adds, how to set it up, how it is built, its tests,
and what to copy from it. Read this guide for the rules, then the module closest to what you want to
build:

| Module | Kind | Read it for |
| --- | --- | --- |
| [`basic`](../../extensions/basic/README.md), [`system`](../../extensions/system/README.md), [`reminders`](../../extensions/reminders/README.md) | Capabilities | The smallest capabilities: declaring one, risk tiers, a `$command`, a store, scheduling |
| [`images`](../../extensions/images/README.md), [`web`](../../extensions/web/README.md), [`media`](../../extensions/media/README.md) | Capabilities | Outside services with keys, fallbacks, files and sources in results, long work with progress |
| [`host`](../../extensions/host/README.md), [`terminal`](../../extensions/terminal/README.md) | Capabilities (terminal also an engine) | Sensitive actions: owner-only, private data, secret paths, approvals and confirmations |
| [`momochat`](../../extensions/momochat/README.md) | Engine | The reference engine: one turn loop for every speaker (tools as function calls or JSON, questions, approvals, resuming after a restart), group chats, hand-offs, a rolling summary, former names |
| [`students`](../../extensions/students/README.md) | Agents, prompt part, watcher, capabilities | Characters as data (one folder each, prompts in plain files, pictures, other names, tiers, a rename that keeps her data), shipping model profiles and a provider, and their memory without importing any engine: a computed `turn` part, a watcher that learns from every conversation engine, facts the pages show, typed-only commands, a required package, a module taken in (`formerly`) |
| [`chimera`](../../extensions/chimera/README.md) | Engine, capabilities, interface | A durable engine written as plain sequential code (replay with a journal), approvals as workflow steps, an engine and its page sharing a store, seats built from other modules' agents (`context.personas()`), and running a module on its own |
| [`cli`](../../extensions/cli/README.md) | Interface | The smallest interface |
| [`discord`](../../extensions/discord/README.md) | Interface | A full platform interface: commands, buttons, files, approvals |
| [`webui`](../../plugins/webui/README.md) | Interface (plugin) | An interface you can drop in: a web page, its protocol and its security |
| [`whale_chan`](../../plugins/whale_chan/README.md) | Interface, capability (plugin) | A complete plugin: its own bot on MomoChat, a student's voice, defaults that make it work as soon as it is dropped in |
| [`wechat`](../../plugins/wechat/README.md) | Interface (plugin) | A plugin that stays dormant until configured, with a desktop transport |

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
| Owner | A sender listed under `owners` in `core.yml` for that interface, e.g. `owners: {discord: ["1234"]}`. Owners decide approvals, run owner-only capabilities, their `$commands` bypass admission and count as their approval. |
| Control route | `control: discord:dm:1234` in `core.yml`. The default private place for approval requests (those an owner did not start in their own direct chat), failure details, events without a route, routine replies without a route, and hidden progress. |
| Route | Gateway data `pattern → {target, kinds}`: replies (of the listed `kinds`, or all) for conversations matching `pattern` go to `target` instead. `kinds` may include `event` to send interface events somewhere other than the control route. Edited with `core.set_route`. |
| Admission rule | Gateway data deciding which person messages become requests (below). Edited with `core.set_rule`. |
| Assignment | Manager data deciding which engine and personas handle a conversation, and their budget (below). Edited with `core.set_assignment`. |
| Permissions | What a conversation may use (access) and what runs there without asking (approval), per tier and per capability (below; [ARCHITECTURE.md](ARCHITECTURE.md#permissions)). |
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
    engine: momochat
    personas: [assistant]          # or persona: assistant (the core's own agent)
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

### Permissions

```yaml
permissions:
  "*":
    rules: {basic.remember: auto}            # exceptions: capability → auto | ask (or {member|admin|creator: …})
  "wechat:*":
    use: ["*", "!system.*"]                  # what may be used here at all
    tiers: {write: always-ask}               # per tier: auto | ask | always-ask (critical: never auto)
  "discord:*":
    rules: {"services.restart(navidrome.service)": {creator: auto}}   # one subject, creator only
```

Per setting (the `use` list, each tier, each rule), the most specific pattern that sets it wins. An
explicit rule is final (a saved "always allow" grant can still turn its `ask` into a run); `always-ask`
admits no exceptions; patterns such as `ext.*` never make a critical call automatic. A module may
suggest defaults (`Contributions.permissions`, or `permissions:` in its own `config.yml`): `use` and
`tiers` only for its own interfaces' addresses, rules only for its own capabilities; `core.yml` overrides
them setting by setting. `$permissions` shows what applies in a conversation, and the grants.
The old assignment keys `allow`, `deny` and `preapprove` are refused in configuration with a pointer
here; runtime assignments saved with them keep their `allow`/`deny` as that pattern's `use` list.

### Agents, students and model profiles

An **agent** is a persona (`contracts.Persona`: who speaks, her prompt, names and pictures) with her model profile.
That is all an agent is: she keeps no memory of anyone. core.yml defines its own under `agents:` (`personas:` before
2026-10-27, still read and merged under it), and any module may add some in `Contributions.personas`; a configured
agent of the same name wins. A **student** is a special agent the students module defines
([`extensions/students`](../../extensions/students)): she always has her memory of each person (her mneme-llm file),
whichever engine she speaks in, so that module requires mneme-llm. Engines take either by name.

```yaml
providers:
  deepseek: {kind: openai_compatible, base_url: "https://api.deepseek.com", api_key_env: DEEPSEEK_API_KEY,
             schema_mode: json_object, effort_param: true, inline_history: true}
profiles:
  default: [{provider: deepseek, model: deepseek-flash}, {provider: openai, model: gpt-6-luna}]
agents:
  helper:                            # an agent of your own: her prompt and her models in one place
    display_name: Helper
    prompt: "You are a careful helper. Answer briefly."
    models: [deepseek/deepseek-flash, openai/gpt-6-luna]   # a profile named `helper`; or `profile: default`
    description: "Helper, a careful assistant"            # optional: one line, for engines that write their own
    aliases: [Help]                                         # optional
  gpt:
    parts: [{name: personality, file: personas/gpt.md}]   # no prompt: parts for a module's agent (a student)
```

A profile is an ordered list of candidates; the next is tried when one fails, or when its answer cannot be
used (blank, not the requested JSON, cut off at the token limit). A transient failure (a rate limit, a 5xx, a
dropped connection) is asked again after 2 and 8 seconds, or after the provider's `Retry-After` (at most 30), before
the next candidate; each retry is `provider_retry` in the audit log. Refusals such as no balance (402) move on at once. A switch is never silent: the result's
`fallbacks` say why each earlier candidate did not answer, the audit log records `provider_failed` or
`provider_rejected` with the reason, and the persona's message carries it (`reply.data["fallback"]`).
`inline_history: true` (on for DeepSeek) sends a JSON request's earlier turns written into one message,
because DeepSeek's JSON mode often answers blank after an assistant turn; a blank JSON-mode answer is
asked once more. `schema_mode` says how an OpenAI-compatible provider is held to the requested JSON: `native`
(a `json_schema` response format), `json_object` (JSON mode, the schema written into the prompt) or
`strict_tool` (the answer as the arguments of one strict tool call: every property required, an object without
properties sent as a JSON string and decoded back, limits checked only here; DeepSeek offers it on
`base_url: "https://api.deepseek.com/beta"`, and its thinking mode needs `tool_choice: auto`, so a reply that
comes as text is read as text). A request with `tools` (`ToolSpec`s) gets the model's own function calls instead of JSON:
`ModelResult.tool_calls`, each checked against its tool's `parameters` (a call to an unknown tool, or with
arguments that do not match, is invalid output, like JSON that does not match), and `replay`, which goes back as it is on
that answer's assistant message in later requests (`tool_calls` and `replay` on assistant messages, `tool`
messages with `tool_call_id`). A replay reaches only the provider that wrote it (DeepSeek's thinking, say,
never another provider after a fallback), and never overwrites the message's own fields. Before any adapter
sees them, the core places each call's `tool` message right after it, answers a call left without one "did not
run", and drops a `tool` message that answers no call, because the APIs refuse such a request. Only adapters with
`supports_tools` take tools (the OpenAI-compatible one, which maps dotted names such as `web.search` to what the
API allows and back). Candidates are tried in order while they can call tools (one without a key is passed over,
as ever): the first that cannot ends the request with `ModelError("tools_unsupported")`, rather than letting
a later candidate answer in its place, so the engine can ask that step in its JSON form; that attempt reached
no provider for its own sake and does not count against the run's `model_calls`. An extension may ship default
personas in `Contributions.personas`; a configured persona with the same name wins.

A persona may also carry (contracts 1.9):

* `aliases`: other names people call her by. A group chat counts "Luna, …" as calling GPT, and the students'
  `$alias luna Lulu` finds her by it.
* `formerly`: persona names she had before. On every start, a former name no persona has now is read as hers:
  core.yml's assignments and `personas: <old name>: parts:` apply to her (the audit says
  `persona_renamed_in_config`; rename them there when convenient), and what the core keeps under the old name
  moves to hers once (`persona_data_moved`, with counts): her memory, every module's stores scoped by her, diary
  notes, `$model` choices, runtime assignments, facts, the runs she is in, and her name on what she said. What she
  already has under the new name wins. A module that keeps its own data by persona name (the web UI's chats, say)
  does the same from `InterfacePort.personas()`. Let open runs finish before renaming someone.
* `pictures`: kind → image file (`avatar`, square; `card`, 3:4; any kind a page knows). Interfaces get them, as
  absolute paths, from `InterfacePort.personas()` and serve them themselves; a picture dropped in an interface's
  own folder may override one.

And since contracts 1.11:

* `description`: one line on who she is ("GPT, the white-haired dragon girl and calm, dependable student-council
  president"), for engines that write their own prompts around her instead of placing `prompt` (Chimera's work
  prompts).
* `profiles`: her model profiles by tier (`contracts.TIERS`) besides `profile`: `own`, her own models with nobody
  else's as a fallback (so the model family answering is hers for sure), and `strong`, her strongest models.
  `persona.tier("strong")` is that profile, else her `own`, else `profile`. Chimera seats a student on her `own`
  tier for cheap work and her `strong` tier for strong work.

A module sees the agents loaded before it with `context.personas()` (contracts 1.11): the core's and those of every
module set up earlier (every module it requires or uses that is there), as `InterfacePort.personas()` describes them,
plus each one's `module`, `description`, `profile`, `profiles` and `models` (`default` and each tier → the
candidates, core.yml's profiles winning). Chimera builds its seats from it. Called after setup, it lists every agent.

### Prices and stamina

Since contracts 1.12 the core prices every model answer: `ModelResult.price` (dollars per million input and output
tokens, and cache hits where listed, with where the figure came from) and `ModelResult.cost` (this answer's dollars,
cache hits at their price), None when no price is known (never a guess). The prices are core.yml's `prices:` over
Artificial Analysis's free list (core/providers/prices.py); a module reads them with `context.prices`
(`contracts.Prices`: `price`, `cost`, `status`, `refresh_if_stale`) and a page with `port.prices()`. Chimera shows
each step's cost and each seat's price from them.

The core also keeps **stamina** (core/manager/stamina.py): what each persona spends a day against her allowance
(core.yml `stamina: allowance`, $0.50 by default; `personas:` per persona; 0 turns it off), what everyone spends
together against the day's total (`stamina: total`, $2.00 by default; 0 turns it off), and the account she spends
from (`accounts:` a balance her provider reports, DeepSeek's, or a budget you enter). Before each model call, a run
when everyone together has spent the day's total, whose persona has spent her allowance today, or that has spent its
own budget (`Request.extra["budget_usd"]`, which the Classroom sets from the team picker), waits for the owner: a
`core.stamina` card ("DeepSeek has spent her $0.50 for today. Let her spend $0.50 more today?"; at the total,
"Everyone together has spent today's $2.00. …"). Approved, the limit grows by `step` and the run carries on where it
was (an engine that saves its state per step goes on; one that starts over makes its calls again); denied, the run
stops with the reason. The wait reaches the core as `Paused`, a BaseException, so an engine's own `except Exception`
lets it pass. A watcher's calls are counted, never stopped. Pages read it with `port.stamina(persona)`: `{today:
{spent, allowance, left, day}, account: {provider, kind, currency, total, left, as_of}}`; `port.stamina()` with no
persona gives everyone's day against the total (`{today: {...}}`), and `port.spending(days)` what was spent each day
(chats, sessions, memories), newest last: `[{day, total, personas: {name: dollars}}]`. The owner's `$stamina`
(`core.stamina_settings`) shows the day and changes the total (`$stamina total 3`), an allowance (`$stamina deepseek
0.8`, `reset` back to core.yml's) or an account's budget (`$stamina budget openai 20`, counted from today); what it
sets is kept in the core's store, over core.yml.

### Prompt parts

A persona can carry prompt parts beside its prompt: its personality, house rules, anything an engine should
place for it. Each persona has its own, set in core.yml or by its module; changing them is an edit of core.yml
or of the part's file, not a command.

```yaml
agents:                           # (`personas:` before 2026-10-27, still read)
  assistant:                      # the built-in agent: its prompt stays, the parts are added
    parts:
      - {name: personality, file: personas/assistant.md}       # relative to core.yml's folder
      - {name: house, text: "Answer in the person's language."}
  gpt:                            # a module's persona (a student): without a `prompt`, only parts are added
    parts:
      - {name: personality, file: personas/gpt.md}
      - {name: season, text: "It is exam season at school.", place: turn, order: 50}
```

* A part is a `text` or a `file`. A file is read each time the part is used, so an edit applies from the next
  turn without a restart; HTML comments (`<!-- ... -->`) in it are notes for whoever edits it and are left out.
  A missing file is not an error: the part is left out until the file exists (the audit says so at start, and
  `python -m core --check` lists it). A file may not be a protected secret path (an env file, a `secrets:`
  entry; a relative entry protects both the path under the home folder and the one beside core.yml): that is
  an error at start, and a part whose file becomes one later is left out.
* `place: stable` (the default) puts the part in the system prompt, after the persona prompt and its name;
  `place: turn` puts it in the turn's context, after the history. Stable parts are the same every turn, so the
  provider's prompt cache keeps serving them; use `turn` only for what changes. Parts come in `order` (100 by
  default, lower first), then as listed.
* `name` defaults to the file's stem, or `part <n>`. A core.yml entry without a `prompt` only adds `parts` to
  the module's persona of that name (to change anything else, give the whole persona, `prompt` included); a
  part named like one the persona already has replaces it, and `text: ""` switches it off. Parts for a persona
  no loaded module has are an error at start.
* At one place, a persona's parts keep to 40,000 characters: the part crossing it is cut, those after it are
  left out. Secrets are scrubbed from parts as from everything else.

A module gives its own personas parts (`Persona(..., parts=(...))`), and may add parts for others with
`Contributions.prompt_parts`: `PromptPart(name, text, place="stable", order=100, personas=())`, for every
persona, or for those in `personas`. Two modules may not add parts of the same name (the second fails to
load). A part's `text` may be a function of a `PartContext` (`persona`, `address`, `trigger`, `owner`, and since 1.9
`engine`, the engine placing it), called
(never awaited) each time the part is used, so it should be quick. A persona's own part shadows a module's part
of the same name. A part that raises or returns something other than text is left out and audited
(`prompt_part_problem`, with the module that added it), once until it renders again; it never fails the run.
Engines get the rendered parts with `session.prompt(persona, place)`; MomoChat places them as above. An engine that never calls it ignores parts.

### Watchers

A module that learns from what is said (memory, mood, closeness: the students' memory) adds watchers (contracts 1.9):

```python
from contracts import Contributions, Watcher

async def learn(exchange, context):
    # exchange: address, persona (a Persona), text (what she said), trigger (the Request she answered), run_id,
    # engine, at, owner (the sender is an owner); exchange.person is (interface, sender id)
    if exchange.trigger.origin != "person":
        return
    result = await context.models.generate(...)          # her profile unless the request names one
    context.store(persona=True, person=True).set("seen", True)
    context.facts.set(exchange.persona.name, "mood", {"face": "good"}, person=exchange.person)

def setup(context):
    return Contributions(watchers=[Watcher("memory", learn, personas=("gpt", "gemini"), timeout_s=120)])
```

* `run(exchange, context)` is awaited after each message a persona says, once it is delivered and recorded,
  whichever engine she spoke in: one exchange per message (three messages in a run are three exchanges).
  Progress lines, work notes (a reply with a label) and command output give none.
* It runs beside the conversation, never before a delivery. One persona's exchanges reach one watcher one at a
  time, in the order she spoke. A failure or a timeout (`timeout_s`) is written in the audit log
  (`watcher_failed`) and nothing else happens; the core's stop cancels watchers still running.
* `context.models` calls models with the persona's profile unless a request names another; the calls are added to
  the run's usage (`watcher_calls`, tokens) but not counted against its budget, since the run may be over.
  `context.history` reads the conversation, `context.store(conversation=, persona=, person=)` is the module's own
  store (`persona=True` is the exchange's persona, `person=True` its person), `context.facts` publishes facts,
  `context.log(event, **fields)` writes to the audit log.
* `personas` limits a watcher to those personas (none: every persona). `exchange.engine` lets a watcher keep to
  the engines whose messages are conversation (the students' memory skips Chimera's reports).
  `exchange.personas` names every persona taking part in the conversation (the run's), hers included: more than
  one is a group chat (the students' memory gives what was said there to every student who heard it).
* A turn a persona starts on her own (Whale-chan speaking up) is submitted by her interface with
  `extra["speak_up"]` holding her motive, which people cannot set; a watcher should read it as her own thought, not
  as something the person said.

### Facts

A fact is a small public value a module publishes about a persona, or about a persona and one person: her mood
or level, a person's bond with her, the name they call her (contracts 1.9). Pages show them; nothing else reads
them, so a fact is never a module's private state (that goes in its stores).

* A module writes with `context.facts` (from `setup`, kept for later) or a watcher's `context.facts`:
  `facts.set(persona, key, value, person=None)`, `facts.get(...)`, `facts.delete(...)`; `set(..., None)` deletes.
  A key is lowercase letters, digits and `_`; a value is JSON, at most 4 KB once encoded. `person` is
  `(interface, sender id)`, as `Exchange.person` gives it. A module writes only its own keys.
* An interface reads with `port.facts(persona, sender_id=None)`: `{"<module>.<key>": value}` for the persona,
  with what is published about her and that person of this interface over it. It reads only its own people.
* Pages look for the names they know, whichever module publishes them: `alias` (a string), `bond`
  (`{"role", "closeness"}`, 0 to 100), `mood` (`{"value", "face", "feelings"}`; `face` is great, good, ok, worry
  or sad), `plans` (`[{"text", "at"}]`, what the person has coming; Whale-chan's speaking up asks about them),
  `called` (`{"name", "asked", "everyone", "setting"}`, what she calls the person and why) and `accounts` (the
  person's accounts, when there is more than one). Chimera publishes `record` (with her `level`) and `benchmark`.
  Stamina is not a fact: pages read it from the core with `port.stamina` ([Prices and stamina](#prices-and-stamina)).
* Facts are kept in the tank (`core:facts`) and last across restarts. Publish again at start what your own data
  already holds, so a page shows it before anyone talks.

### Model providers

A module may bring the models it needs. A vendor with an OpenAI-compatible API (most of them) needs only
a provider of the built-in kind `openai_compatible`; a vendor with an API of its own needs a kind: an
adapter class (`contracts.ModelAdapter`) whose `complete(...)` makes one request and returns an
`AdapterResult` with the raw text. The core does the rest: fallback, schema validation, spacing, images.

```python
import os

from contracts import AdapterResult, Contributions, Extension, NotConfigured, Setting

class Acme:                                    # contracts.ModelAdapter
    def __init__(self, settings):              # the provider's settings, as in core.yml
        self.key = os.getenv(settings["api_key_env"], "")

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        if not self.key:
            raise NotConfigured("ACME_API_KEY is not set")       # this candidate is skipped
        ...                                                       # one request to the API
        return AdapterResult(text, input_tokens, output_tokens, truncated)

def setup(context):
    return Contributions(adapters={"acme": Acme},
                         providers={"acme": {"kind": "acme", "api_key_env": "ACME_API_KEY"}},
                         profiles={"acme_chat": [{"provider": "acme", "model": "acme-large"}]})

EXTENSION = Extension("acme", setup, contracts="1", settings=(
    Setting("acme_api_key", "Acme API key", "secret", env="ACME_API_KEY"),))
```

* Kinds register first, then providers, then profiles, so each may use the ones before; core.yml's
  providers may use a module's kind too. A kind that already exists (built-in or another module's) refuses
  to load. An adapter may also define `async list_models()`, returning IDs or (ID, release date) pairs;
  `$model` shows them, narrowed by the provider's `models_since`, `models_exclude` and `models_allow` (regular expressions).
* **Keys, the proper way:** name the variable in `api_key_env`, keep the key in `.env`, and declare it
  as a `secret` setting so `python -m core setup` asks for it. Keys kept this way are scrubbed from what
  models see. The built-in kinds also accept `api_key` written in a provider's settings (for example in
  your module's `config.yml`), and your own kind may read its key from anywhere; the core does not look
  for keys kept that way and does not protect them. That is the owner's choice.

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
    # context.facts (what it publishes for pages, contracts 1.9), context.present (which modules it uses are
    # loaded, contracts 1.10), context.log(event, **fields)
    return Contributions(interfaces=[...], engines=[...], capabilities=[...],
                         personas=[Persona("whale", "本鲸", "…", profile="default")],
                         prompt_parts=[...], watchers=[...])

EXTENSION = Extension("my_extension", setup, contracts="1", version="1", description="...",
                      requires=("students",), uses=("momochat",))   # see Dependencies below
```

* `name`: lowercase letters, digits and `_`; unique across loaded extensions.
* `setup` may be `async`. It runs once at startup; start network loops in `Interface.start`, not here.
  Objects created in `setup` may be shared by your interface, engines and capabilities.
* `contracts="1"` is the contracts major version you target; a mismatch refuses to load.
* **Loading:** every folder with an `extension.py` in `extensions/` (built-in) and `plugins/`
  (external) loads at startup, built-in ones first; drop a folder into `plugins/` and restart. Folders
  in `plugins/` and every `config.yml` are git-ignored (except the tracked `whale_chan` example and
  `wechat`); a module that is its own repository should also ship a `.gitignore` listing `config.yml`.
  [`plugins/whale_chan`](../../plugins/whale_chan) is a complete example: a second Discord bot with
  its own engine and persona. A module every install gets but few use should stay dormant until it is
  configured (as `wechat` does: no contributions without settings).
* `core.yml` turns modules off and adds folders from elsewhere (external):

```yaml
modules:
  exclude: [extensions/terminal, whale_chan]   # a path ending or a folder name; never imported
  paths: [../MusicManager/bot_extension]
extension_config:                     # rarely needed: the module's own config.yml is the place
  my_extension: {some_option: 1}      # merged over that config.yml into context.config
```

* **Settings:** a module's own settings live in its folder: it ships a documented
  `config.example.yml`, and the machine's copy is `config.yml` next to it (git-ignored). `core.yml`
  holds what is shared (owners, models, personas, admission, assignments); `extension_config.<name>`
  there, when present, is merged over the folder's `config.yml` into `context.config`.
* **Declaring settings** makes them settable by `python -m core setup` and `$setup` (and documents
  them). `Extension(..., settings=(Setting(...), ...))`; the declaration is read without running `setup`:

```python
Setting("token", "The bot's token (Developer Portal → Bot)", "secret",
        env=("MY_BOT_TOKEN",), ask=True, check=check_token)   # async check(value) -> note, or ValueError
Setting("channels", "Channel IDs to work in", "list", [])     # kinds: text, secret, number, boolean, list, path
```

  `key` is the key in your `config.yml` (read it from `context.config` as usual). A `secret` goes only
  to `.env` under the first `env` name (read it with `os.getenv`), is typed hidden, and is never shown
  or accepted in a chat. `ask=True` puts it in the first-run questions; `check` tries a new value (the
  Discord module logs in with a token and returns the bot's name). Declare what people actually change.
* **Only secrets go in `.env`:** the core treats every value there as a secret and blanks it out of
  anything shown, wherever it appears (a model name kept there would vanish from replies). Ordinary
  settings (paths, URLs, IDs, names) belong in your `config.yml`; read them from `context.config`, not
  `os.getenv`.
* **Secrets:** prefer `.env` and name the variable in the config (for example
  `token_env: DISCORD_BOT_TOKEN_WHALE_CHAN`). Putting a secret directly in the folder's `config.yml`
  (`token: …`) is supported for people who would rather not edit the environment; then
  `chmod 600 config.yml`, and never share or copy the folder together with its `config.yml`.
* **Defaults that make a drop-in work on its own:** `Contributions` may also carry model `providers`
  (name → settings, as in core.yml; over the built-in settings, and under those core.yml gives, key by key), model
  `profiles` (name → ordered candidates, as in core.yml; they may use the module's own providers), and
  `assignments` / `admission` for the extension's own interfaces only (`<interface>:*` or
  `<interface>:<conversation>`; anything else leaves the module out), and
  `permissions` (`use` and `tiers` for its own interfaces, `rules` for its own capabilities; whale_chan
  makes every write ask in her conversations). The module's own `config.yml` may set `permissions:` under
  the same limits. core.yml and runtime edits always win.

`python -m core --config core.yml --check` loads and validates every module, marks it built-in or
external, and lists what each contributes and declares, without starting anything. It exits with 1 when a module
was left out (see below), though the core itself would run.

### Dependencies

Since contracts 1.10 every module declares, in its `Extension(...)`, the other modules it builds on, by module name
(the `name` in their `EXTENSION`):

| Field | Meaning | Without it |
| --- | --- | --- |
| `requires` | The module cannot work without these. A Python package is `package:<name>` or `package:<name>>=<version>` (the distribution's name, as pip knows it). | The module is not loaded, `--check` says why, and every module that requires it is left out too |
| `uses` | The module works without these, minus what they would add. | It loads; `context.present` says which of them are there, so it offers only what it can |

```python
EXTENSION = Extension("webui", setup, uses=("momochat", "students"))        # its chats need both; the page opens without
EXTENSION = Extension("whale_chan", setup, requires=("students", "momochat"))  # her bot is a student's, on MomoChat
EXTENSION = Extension("students", setup, requires=("package:mneme-llm>=0.2",))  # a student has her memory
```

* **Declaring is not optional.** After setup, the core checks every engine and persona your suggested
  `assignments` name. Each must be your own, the core's, or one a module in your `requires` or `uses` adds;
  otherwise your module is left out with "names the engine 'momochat' of the momochat module, which it does not
  declare".
  You find out the first time you run it.
* **Order:** a module loads after the modules it requires or uses that are there, so `context.present` is known at
  setup. Modules that depend on each other in a circle are both left out.
* **A suggested assignment naming a module you `use` that is not there** is set aside (`default_unusable` in the
  audit log, a line in `--check`), instead of stopping anything. Better: leave it out yourself, from
  `context.present`, as the web UI does.
* **A module that cannot load is left out, never the whole core:** an import error, a failing `setup`, contributions
  of the wrong type, a name another module already registered, a default for someone else's interface. The audit
  log records `module_skipped` with the reason, the console prints `not loaded: <module>: <reason>`, and `--check`
  exits with 1.
* **What stays fatal:** what the owner wrote. An assignment in core.yml (or set at run time) naming an engine or
  persona that is not loaded stops the core with that message, as do two modules of the same name.
* **The core needs no module.** Its own default assignments (`*` and `routine:*`, to the `momochat` engine) apply
  only while that engine is loaded. With no engine at all, the typed `$` commands still work through any
  interface, and a plain message in the owner's own direct chat gets one line saying nothing answers there.

The architecture page shows each module's declarations beside its description.

### Former names

Renaming an engine or a module keeps what was under the old name (contracts 1.11):

| Field | What the core does |
| --- | --- |
| `EngineSpec(..., formerly=("room", "assistant"))` | An assignment naming an old name (in core.yml, set at run time, or a module's default) is read as the engine (`engine_renamed_in_config` for core.yml's, until you rename them there; runtime ones are rewritten), and its open runs carry on in it (`engine_runs_moved`), given the state the old engine saved: an engine that cannot read it should end the run quietly. |
| `Extension(..., formerly=("companion",))` | Its data folder under an old name becomes its own when it has none yet (`module_data_moved`; with data in both, the old one is left and `module_data_kept` says so). Settings written for an old name, `config.yml` in a folder of that name beside its own and `extension_config.<old>` in core.yml, are read under its own, which win (`module_settings_moved` until you move them). Its stores and facts move to it (`module_renamed`). A module declaring an old name in `requires` or `uses` is given this one, and its `context.present` names it as it was declared. |

A module may take in another one this way, as the students module took in the companion.

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

Three interfaces to read: [`extensions/cli`](../../extensions/cli/extension.py) (the smallest),
[`plugins/webui`](../../plugins/webui/interface.py) (a web page: its own server, a transcript, files both
ways, approval cards and progress as live "typing", with the guards a creator-level page on the network
needs) and [`extensions/discord`](../../extensions/discord/interface.py) (a full platform).

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
| `set_owners(ids, control=None, *, fallback=False)` | Report the platform's own owner, e.g. a bot application's owner. Used only when `core.yml` lists no owners for your interface; `control` (one of your addresses, such as the owner's DM) is used only when no control route is configured. This is how a fresh Discord setup routes approvals to the bot owner's DM without configuration. `fallback=True` offers `control` only for when nothing else does: the CLI, at an interactive terminal, reports its user as the owner and itself as the fallback, so a machine with nothing connected still gets approvals at its console. |
| `set_ready(ready, detail="")` | Report whether you can send. The first call with `False`, and every change, sends an `interface_ready` / `interface_not_ready` event (with `data["detail"]`) to the owner. While not ready, deliveries to you are refused (`Delivery(ok=False, detail="interface_not_ready")`, not uncertain). Interfaces start ready. |
| `bottle` | Your private persistent store (below). |
| `commands()` | The command catalog (`Command` declarations: built-ins, capability and operation commands) for a native command menu or your own help. Translate a menu selection into its `$` text with `command.render(values)` and submit that as the request's text. |
| `log(event, **fields)` | Audit log entry (no message text, please). |
| `models_ready(profile) -> bool` | (contracts 1.7) Whether some candidate of a model profile can be called (its provider has a key), so a page can show who can answer before anyone asks. Engines and capabilities have `models.ready(profile)`. |
| `publish(link=None, status=None)` | (contracts 1.7) Tell the other interfaces where people reach you (`link`, an http(s) URL) and a small public status (a JSON object, at most 16 KB), so they can link to you and show what happens here. Each call replaces what it names. A status that describes a place follows `contracts.PLACE_STATUS`: `{"place": {"name", "icon", "note"}, "people": [{"key", "name", "avatar", "doing", "title"}], "away": [keys of people from elsewhere who are here now], "tickets": [{"key", "by", "title", "link"}]}`; any interface can then draw any other place (the web UI's mini map draws the Classroom this way, and the Classroom draws MomoChat). A key is the name lowercased with only its letters and digits kept, in any script ("Flash-chan" → "flashchan"); an empty key matches nobody. The status must be plain JSON (no NaN or Infinity) and a place's lists must have that shape, else `ValueError`. Kept in memory while the core runs; publish again at start. |
| `directory()` | (contracts 1.7) What every interface has published: name → `{"link", "status", "ready"}` (copies). |
| `approval_status(approval_id) -> str \| None` | (contracts 1.7) `pending`, `approved`, `denied` or `expired` for an approval asked in one of your conversations (None for any other). A pending one whose run is no longer open counts as expired. Ask it before offering an approval card's buttons: a card may have been decided elsewhere (typed, in another interface) or have expired with its run. |
| `personas()` | (contracts 1.9) Every persona, in the order modules gave them: `name`, `display_name`, `aliases`, `formerly`, `pictures` (kind → absolute image path) and `ready` (some candidate of her profile can be called). A page shows the students from this rather than keeping its own list. |
| `facts(persona, sender_id=None)` | (contracts 1.9) What modules publish about a persona, and about her and one of your people (see [Facts](#facts)). |

All port methods act only on your own interface's addresses (except `directory`, which only reads); anything else
raises `ValueError`.

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
| `mentions_me` | For groups: `True` only if the message explicitly addresses this account. What counts (a mention entity, a quote-reply to the bot, a typed known `$command`) is your interface's decision; prefer platform metadata over text matching. `None` if unknown. |
| `reply_to`, `quoted_text` | ID and text of the message this one quotes or replies to, if any. |
| `display` | What this conversation can show; usually `self.display`. |
| `extra` | Opaque string facts for your own use; the core stores but does not interpret them. On requests the core creates for a conversation routine it sets `routine` (the ID) and `routine_via` (`command` if a person typed it, `tool` if a model set it up through `core.add_routine`); an engine that acts without asking, like the terminal, should refuse `tool`. |

`origin` stays `"person"`; only the core creates routine requests.

`submit` returns a `SubmitResult`: truthy with `reason` `accepted`, `command` or `relayed` (an owner
message sent on), or falsy with `duplicate` or the admission decision (`disabled`, `not_mentioned`,
`sender_not_allowed`, `owner_only`, `before_rule_change`, `too_old`). If it raises, nothing was
accepted; submit it again later. **Advance a read cursor only
after `submit` returns.** Submit messages of one conversation in order; different conversations may
be submitted concurrently. Messages starting with `$` and a letter are commands (subject to the
`commands` rule): `$approve <id>`, `$deny <id>`, `$runs`, `$cancel [run-id|address]`, `$tools`,
`$memory` (anyone: read, forget or clear what personas keep about them in this conversation),
`$admin` (admins: status, list, lock/unlock, own view on/off; the creator also adds and removes admins and can `$admin restart` a core run by its supervisor),
`$routine` (anyone: repeat a message or command on a schedule; see Routine examples),
`$setup` (the creator: modules on or off, and their declared settings; secrets only on the machine with
`python -m core setup`; `$setup` lines are not kept in history; changes apply after a restart),
`$help [command]`, `$call <capability> <json arguments>` (owners only: run any capability directly, for
example `$call core.set_rule {"pattern": "wechat:x", "rule": {"enabled": true}}`; counts as approval), plus
capability and operation commands (`$help` lists them). The core only understands this `$` text;
platforms with their own command menus (Discord slash commands) translate into it. Whether a typed
`$command` addresses the bot in a group is likewise your interface's decision, expressed as
`mentions_me` (the included Discord interface counts a command named in `port.commands()`). The core has
no per-platform exceptions: fit a platform's habits to these facts in its interface.

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
| `text` | `reply.text`. If `reply.quote` is set (only when `display.quotes` is true), quote that request ID. Split long text if needed. If `reply.speaker` is set (engines with several personas), name the speaker, with `reply.label` if any (Discord: `**Luna · Work note**` above the text). A persona's message (or any message of a one-persona run) has `reply.data["provider"]` and `reply.data["model"]`, the model that wrote it, and `reply.data["fallback"]` when that was not her first candidate (why each earlier one did not answer); a `$command`'s answer has its result's data as JSON in `reply.data["result"]` (`$model` lists each persona's model and the providers' models there). Showing them is optional. |
| `file` | Send `reply.file.path` (named `reply.file.name`), caption `reply.text`. The core already checked `display.files` and `max_upload_mb`. |
| `progress` | A transient status line, one per run (`reply.run_id`): edit it in place where you can, and clear it when the run answers. It carries a `Cancel` action (`$cancel <run>`), which stops the run's work (an engine's turn or a `$command`'s capability). You get it only if your `display.progress` is true or you are the control route. |
| `notice` | Phrase `reply.code` in your platform's style (table below). |
| `event` | An interface event for the owner (you receive these if you host the control route or an event route). Show `reply.text`; `reply.data["origin_address"]` and `reply.data["interface"]` say where it came from. Return the `message_id` so owner replies can be relayed. |

`reply.source` says who produced it: `run`, `command`, `owner` (a relay or owner send) or `core`.
`reply.in_reply_to` is the triggering request ID; `reply.run_id` the run. The `$cancel` confirmation
also names the run it cancelled in `reply.run_id`, so clear that run's progress then. Replies routed from another
conversation carry `data["origin_address"]`.

| Notice `code` | When | `data` |
| --- | --- | --- |
| `restarted` | The core restarted (control route, and for `$admin restart` also the conversation that asked); `text` says why (requested, crash, hang) and how long it was down. | `reason` |
| `run_failed` | The request could not be finished. Show a short neutral message in the conversation's language (or suppress it). | `error_code` |
| `run_failed_detail` | Details for the owner (control route). `text` is English. | `error_code`, `address` |
| `approval_request` | An action needs the owner's approval, or the owner's own command asks to be confirmed (in the owner's direct chat where they asked, else the control route). `text` is an English summary written by the core; `actions` are Approve/[Always allow]/Deny or Confirm/Cancel. | `approval_id`, `capability`, `address`, `arguments` (JSON, scrubbed), `tier`, `kind` (`approval` or `confirm`), `summary`, `warnings` (` \| `-separated), `always` (`true`/`false`), `asked_by` |
| `secret_refused` | A model's call was refused because it would have read or sent a secret. `text` says what. | |
| `waiting_approval` | A `$command` is waiting for approval. | `approval_id` |
| `relayed` | An owner reply to an event was sent on. | `address`, `ok`, `detail` |
| `unknown_command` | Unknown `$command`. | `command` |
| `not_permitted` | The command is not available here. | |
| `locked` | The bot is in maintenance (`$admin lock`): a person who is not an admin wrote; told once per conversation. | |
| `reminder` | A reminder (`Context.schedule`) is due; `text` is the reminder. | `scheduled_id`, `sender_id`, `sender_name`, `at` |

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
capability with that exact name, loading fails. `risk` and the attributes (`private`, `subject`,
`subject_kind`, `never_auto`, `grantable`, `confirm`) work as for capabilities; `command`/`command_argument`
add a `$command`; `timeout_s` defaults to 120. `operate` returns a `ToolResult` (attach a QR image in `files`). For results that arrive later
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
| `risk` | The tier. `read`: observes only; may run again. `prepare`: creates local artifacts (downloads, renders); runs once per run. `write`: changes something reversible inside the bot (reminders, notes, routines); asks by default. `critical`: affects the machine, other people or permissions (shell, saving files, services, sending as the owner, policy); asks by default and is never made automatic by a tier setting or a pattern. Writes and critical actions run once per run. Declare honestly; the permissions depend on it. |
| `owner_only` | Only owners (or trusted routines) may use it (same as `access="creator"`). |
| `private` | Shows the owner's private data: offered only in conversations private to the owner; that run's output never follows a route elsewhere, and it asks before any critical action. |
| `subject`, `subject_kind` | The parameter rules and grants match on (`"unit"`, `"chat"`); `subject_kind="path"` matches globs and makes the core refuse secret paths (`.env`, keys) to models. |
| `escalate` | `escalate(arguments)` may return a higher tier for one call (never lower), e.g. `critical` for `rm`-like options. |
| `never_auto` | A model's call always asks, whatever the permissions say (policy changes, accounts, shell lines). |
| `grantable` | The owner may answer with "always allow" for that exact subject in that conversation (needs `subject`). |
| `confirm` | Even the owner's own typed command asks for a one-tap confirmation. |
| `describe` | `describe(arguments)` returns one plain line for approval prompts ("Restart navidrome.service"). |
| `needs` | `Display` features the conversation must have, e.g. `{"files"}`. Whether incoming attachments exist is not a feature; capabilities that read them check the request's `files` themselves. |
| `internal` | Callable by engines but not shown to models. |
| `command`, `command_argument` | Also available as `$command <text>`, with the text in that parameter. An owner's `$command` counts as approval of that exact call (except `confirm`, a secret in the arguments, or private data in a shared conversation, which ask to confirm). `command` is a name, or a `Command` declaration (below). |
| `timeout_s` | Hard limit for one call (default 600). |

**Access levels.** `access="admin"` or `"creator"` (or `owner_only=True`) limits who may call a
capability at all; `context.access` (`member`, `admin`, `creator`) is the caller's effective level, so
one capability can also behave differently by level, for example listing only the caller's own items
for members and every conversation's items for admins. Describe the admin behaviour in the command's
`admin_help`; `$help` shows it to admins only.

The handler receives validated `arguments` and a `ToolContext`: `address`, `sender`, `origin`,
`run_id`; `display` (what the calling conversation can show, e.g. `max_upload_mb` for fitting files);
`history` (contracts 1.6: the calling conversation's history to read, `recent`, `search` and `summary`, as engines see
it; for capabilities that carry a conversation elsewhere or work from it, such as Chimera's `chimera.invite` to the
Classroom; a capability cannot change it);
`files` (files people attached to the run's requests; `own_files`: only those of the person who started the run, which is what anything writing them somewhere should use); `storage` (your extension's private store, shared by your capabilities, the same as `store()`; `store(conversation=, persona=, person=)` scopes it, see [Storing data](#storing-data); committed on
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

A run that is cancelled raises `asyncio.CancelledError` in your `run`. When the core is only stopping (it will
replay the run on the next start), the error carries `contracts.SHUTDOWN` in its `args` (contracts 1.7): an engine
that keeps state of its own (a task board, say) should leave it as it is then, and record a cancellation only for
any other one (`$cancel`, a conversation cancelled).

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
| `personas` | Personas of this conversation. |
| `store(conversation=, persona=, person=)` | Your module's key-value store, scoped by any combination of this conversation, one of these personas (across conversations) and the trigger's sender (across conversations); no arguments: the module's store everywhere. See [Storing data](#storing-data). |
| `memory(name)` | Older: a persona's store shared by every module running it; prefer `store(persona=name)`. |
| `prompt(name, place="stable")` | That persona's [prompt parts](#prompt-parts) at `stable` or `turn`, rendered in order and joined ("" when it has none). |
| `diary(name)` | That persona's notes about the trigger's sender, in this conversation or across those sharing its `diary_scope` (see below). |
| `history.recent(limit)` | This conversation's history, oldest first: every admitted request and `$command`, messages interfaces `observe`, relayed owner messages, and successfully delivered replies and files (`direction="out"`, `reply_to` = the request they answered). Non-admitted messages, notices, progress and failed or uncertain deliveries are not recorded. Items keep their attachments in `files` (request files, observed files, delivered files). Filter by `sender_id` yourself if needed. |
| `history.search(text, limit)` | This conversation's messages containing `text` (any case), newest first, at most 100: further back than `recent` reaches (200). |
| `history.summary()`, `history.summarize(text, upto)` | The conversation's rolling summary (`HistorySummary`: `text`, `upto`, `updated_at`), shared by every engine and persona in it. An engine compacting older history writes it (1 to 8000 characters; secrets are scrubbed and what is stored is kept within 8000) covering messages up to `upto`, a finite time (otherwise `ValueError`); people read and clear it with `$memory`. |
| `inbox()` | Requests for this run's lane that arrived while this run was thinking; each is returned once. Messages arriving while the run waits on a capability or a file delivery start their own run instead. |
| `activity()` | Other open runs in this conversation (`Activity`: `run_id`, `status` `working`/`running`/`waiting_input`/`waiting_approval`, `sender_id`, `sender_name`, `request`, `doing`, `since`), so an engine can say "still converting" instead of starting the same work again. |
| `models.generate(ModelRequest)` | Model calls (default profile: the first persona's). With `output_schema`, `result.data` is parsed and validated JSON. `images` (up to 6 local JPEG/PNG/GIF/WebP files, for example the request's attachments) are shown with the last user message; candidates whose provider has `vision: false` are tried last and told images were attached. `persona` (one of the session's) says whose call it is, so her `$model` choice here applies and her message can say which model wrote it; engines with several personas should set it (without it the core uses the run's only persona with that profile, or none). `result.fallbacks` says why each candidate before the one that answered did not (empty when the first did). `result.cached_input_tokens` (contracts 1.7) is how many of `input_tokens` the provider read from its prompt cache, which it bills far cheaper; `effort` may be `max` (1.7) for providers that think longest at it. Raises `ModelError` (`unavailable`, `invalid_output`, `budget_exceeded`, `not_configured`). `models.ready(profile)` (contracts 1.6) says whether some candidate of a profile has its provider configured, so an engine can pick who works before trying. |
| `tools.list()`, `await tools.call(name, args, call_id)` | Capabilities allowed here, and calling them under policy. Never raises for policy; check `result.status` (`success`, `failed`, `needs_input`, `needs_approval`). |
| `await reply(text, quote=None, persona=None, label="")`, `await send_file(file)`, `await progress(text)` | Output; returns the interface's `Delivery` (or a refusal such as `run_closed`, `conversation_disabled`, `too_large`). `persona` (one of the session's) makes the persona's display name the reply's `speaker` and the sender name in history; `label` qualifies it (for example a work note, recorded with kind `note`). |

Return one of:

* `RunYield.DONE`: finished. Unread inbox messages start a new run.
* `RunYield.WAIT_INPUT`: you asked a question; the next message in this lane resumes this run as an
  `input` event. Use `lane: sender` in groups so only the person you asked can answer.
* `RunYield.WAIT_APPROVAL`: a tool call returned `needs_approval`. The owner's decision resumes the
  run with a `tool_result` or `approval_denied` event.

Exceptions fail the run: the conversation receives a `run_failed` notice and the owner the detail.

A running run that is cancelled (`$cancel`) gets `asyncio.CancelledError` inside `run`. A run that is waiting
(for input or an approval) has nothing running to receive it, so the core calls `stopped(session, reason)` instead:
optional, with the run's last saved `state`, for putting right what still shows the work as going.

### Writing a resumable engine

`run` is called again after a restart, after an answer and after an approval. Make it replay safely:

* Keep everything you need in `state` and `save` after each meaningful step.
* Give every tool call a deterministic `call_id` and save it **before** calling. Calling again with
  the same `call_id` returns the stored result instead of running again; an interrupted prepare or
  write returns `uncertain_previous_attempt` rather than repeating. Identical prepares/writes without
  a `call_id` also run only once per run.
* Track which events you have processed (by index).
* Prefer at-most-once for messages: save that you are about to speak, then speak.

### The built-in `momochat` engine

[`extensions/momochat`](../../extensions/momochat/README.md) is the conversation harness: direct chats, group chats,
tools and questions, for students and agents alike. It was three engines until 2026-10-27, `assistant`, `chat`
(Whale-chan's) and `room`; those are its former names (`EngineSpec.formerly`), so assignments naming them reach it.
The core's own defaults (`*`, `routine:*`) assign it with the core's `assistant` agent. Read its source as the
reference engine: state, events, tool calls, approvals, questions, images and resumable work in one place.

* **Who speaks.** One persona: she does. Several (a group chat): those the message calls on ("Luna, …", "@Luna"),
  everyone for "大家", "everyone" and similar, those it names in passing may add a word after the answer, and a
  message naming no one goes to whose turn it is (a rotation kept per conversation); with `chime_in` the others may
  join in after her. Joining in is one short remark or `pass` (stay silent): no tools, questions or hand-offs.
* **One turn for every speaker.** Each step is her words and her calls: as function calls where her model can make
  them (`tool_calls`, on by default), otherwise one JSON object `{message, status, actions}`, validated with
  corrections; the transcript keeps both, so a step can fall back. The engine's own tools: `ask` (one question,
  then `WAIT_INPUT`), `pass` (only when invited to join in) and, in a group, `ask_classmate` (the hand-off): the
  classmate takes a turn of her own with the request right after, with her own tools, and the asker then gets a
  short turn to finish with her answer. Actions run in order with saved `call_id`s, a batch stops at the first
  failure, files from successful results are sent, `needs_approval` waits durably.
* **What she reads.** Her system prompt (her prompt, display name, stable prompt parts, `instructions`, the
  contract, and in a group the roster and the group's rules), the thread since the last `/new` (the last
  `history_limit` messages word for word, `history_chars` at most if set, older ones in the conversation's rolling
  summary, written by one `compact_profile` call once `compact_every` have scrolled out; `$commands` and their
  answers left out), then the turn's context (the time, her turn parts, such as a student's memory of the person,
  other open runs, what was said in this run before her, why she speaks if she was not the one asked), the message,
  and her turn so far. Images attached, sent shortly before (`history_image_minutes`) or found by tools are shown.
* **Two styles.** `brief` (the default): she speaks as she goes, in her own voice. `full` (a personal AI chat client,
  as Whale-chan's bot uses it): complete answers in Markdown, her notes while working shown as progress and only the
  answer posted. `/new` (`/reset`, `/clear`) starts a fresh thread in any conversation.
* **Her own impulse.** A request an interface marks `extra.speak_up` (with its text starting `⟨speak-up⟩ `) is her
  own wish to message someone: one step, no tools, from the motive.
* **It keeps nothing of people.** No core diary: a student's memory is the students module's (its prompt part and
  watcher), and an agent has none.
* Assignment options: `instructions`, `owner_instructions`, `style`, `effort`, `max_steps` (per speaker),
  `max_turns`, `max_handoffs`, `max_tokens`, `timeout_s`, `tool_calls`, `history_limit`, `history_chars`,
  `history_scope: sender`, `history_image_minutes`, `compact`, `compact_every`, `compact_profile`, `chime_in`,
  `new_chat_text`, `quote: latest|trigger`. Module settings (`extensions/momochat/config.yml`) are their defaults.

### Diaries

No engine in this repository keeps a diary any more (a student's memory is the students module's; an agent
keeps nothing); the core still offers it to engines that want one.

`session.diary(persona)` returns a `Diary` about the person who sent the trigger, in this conversation
only by default; a DM disclosure never shows up in a server. A note is always filed in the conversation it
was written in. An assignment's `diary_scope: <name>` option makes a conversation also read the notes of every
other conversation whose assignment sets the same name (the same character remembering the same person
across them, like the web UI's chats, which only their owner reads). The scope is only a filter: turning
it on, off or to another name moves nothing, so notes are never stranded. It spans one interface, since a
sender ID names a person only there. A note written under a key that another conversation of the scope
already holds updates that note where it is; if two hold the same key (from before they shared),
`recall` gives the newest. `$memory`, `forget` and `clear` cover the whole scope. (Notes an earlier version filed under the scope
itself are filed once, at start-up, in the scope's conversation their person spoke in last.) A scope name is text (a
number counts as its digits); anything else, `false` included, means none. `recall(text, limit)` returns `DiaryNote`s, those
matching words of `text` first. `write(key, kind, note, importance)` creates or replaces the note
under `key` (`kind` is one of `DIARY_KINDS`: fact, episode, reflection). The core refuses
(`ValueError`) invalid keys or values, notes over 400 characters, notes that copy the message instead
of paraphrasing it, and more than 4 changed notes per run; it keeps at most 200 notes per persona and
person in each conversation, dropping the least important oldest. Routine runs have no person: `available` is false,
`recall` is empty and `write` raises. People manage their notes with `$memory`, so engines need no
commands of their own for this.

### The included Discord interface

**Slash commands.** At login the interface registers the core's command catalog as Discord
application commands (global, so they work in servers and DMs; re-registered only when the catalog
changes). An invocation is deferred within Discord's 3 seconds ("thinking…"), translated into the
command's `$` text with `Command.render` and submitted as the invoking user's request; the first reply
replaces the placeholder (progress edits it), later replies follow up, `private` replies are
ephemeral, and after 15 minutes replies go to the channel as usual. Commands with a `message_action`
appear under Apps on a message (`/video`: "Download video"). `Reply.actions` become buttons whose
presses run their `$command` as the presser. Creator-only commands are hidden from members in servers.
Typed `$` commands keep working, and in servers a typed command from the catalog (`$help`, `$remind …`)
addresses the bot without a mention, like its slash form; unknown ones (`$AAPL is up`) stay ordinary
chatter. Option `slash_commands: false` turns registration off.

**Progress, reactions and threads.** A run's progress is one message edited in place (inside the
slash command's placeholder when it came from one) with a Cancel button; the answer, or the
cancellation, removes it. Reacting to a message with buttons presses one, as the person reacting:
✅ or 👍 the first success/primary button (Approve), ❌, 🛑 or 👎 the first danger button (Deny,
Cancel). In the channels listed in `thread_channels`, a message addressed to the bot that is not a
command opens a thread on it (named after it, archived after a day idle), and the run happens in the
thread's own conversation. The thread starts fresh: the bot sees the message, not the channel's earlier
history. In any thread the bot opened, and in any thread someone starts on one of the bot's messages
(anywhere, to follow up on an answer), every message counts as addressed, so follow-ups and answers to
its questions need no mention. A thread has its own address (`discord:<thread id>`): its parent
channel's admission rules do not apply there, so restrict threads with `discord:*` patterns if
needed. Thread channels need the Create Public Threads permission (included in the invite link).

[`extensions/discord`](../../extensions/discord/interface.py) renders replies with Markdown, speaker
labels, a rolling status block for unrequested notices, and math: inline `$…$` becomes Unicode and
display `$$…$$` is sent as PNG images between the text parts. Its public operations (usable by
anyone, acting only on the calling conversation) are `discord.music` (`$music`), `music_status`,
`music_control`, `queue_music` (one player per server, in the caller's voice channel), `time`
(`$time`), `clock_start` (`$start`) and `clock_stop` (`$stop`); `status` and `invite` are owner-only.
Settings (`extensions/discord/config.yml`, see its `config.example.yml`): `talk_prefixes` (for example `["$agent", "$chat"]`: a message starting with one is addressed
to the bot like a mention, without the prefix), `thread_channels` (channel IDs where tasks get threads), `music_dir`, `music_list`, `ffmpeg`, `clock_interval_s` (1), `clock_max` (10), `math` (true),
`math_width_px`, `math_dpi`, `math_font_size_pt`, `math_min_height_px`, `math_max_images` (10).

### The included terminal

[`extensions/terminal`](../../extensions/terminal/extension.py) is a shell on the bot's machine for the
creator only. It loads with the other built-in modules; turn it off with
`modules: {exclude: [extensions/terminal]}`. Each conversation has its own long-lived shell
(PowerShell on Windows, bash on Linux), so `cd` and variables persist between inputs. There are three
ways in:

* `$t <line>` from any conversation (also `/t`): `$t` alone shows output that appeared since, and
  `$t ^C` interrupts what is running while keeping the shell. On Linux that is a real Ctrl-C (the shell
  is an interactive bash without rc files, so loops stop too). On Windows the running programs are
  ended; if there are none (a loop in PowerShell itself) the shell is, and the next line starts a new
  one. A typed `$t` needs no mention on Discord.
* A terminal conversation, where every message is a line for the shell. In a terminal conversation,
  type a line starting with `$` and a letter as `$t $HOME`; otherwise it is read as a command.
* Models: in a run the creator started, a MomoChat speaker can use `terminal.send` (one line; critical and
  `never_auto`, so each line waits for the creator's approval, which shows the exact line, whatever the
  permissions say) and `terminal.peek` (new output; a private read, no approval). This is how "ask the
  bot to do something on my computer" works. Nobody but the creator can reach the shell, by command,
  terminal conversation or model.
* The shell runs as the bot's OS user with the bot's environment. The core's secret filters only cover
  what passes through the bot (see ARCHITECTURE.md, Enforcement: secrets); what a line itself reads or
  sends is bounded by that approval and by the operating system.

```yaml
assignments:
  "discord:123456789012345678": {engine: terminal, persona: terminal}
admission:
  "discord:123456789012345678": {require_mention_in_groups: false, owner_only: true}
```

After each input, the output is shown as it arrives (a progress message updated every few seconds;
its Cancel button interrupts the program), then the last screenful once nothing new has appeared for
`quiet_s` seconds or after `max_wait_s`. A program still running is named in the reply. Lines typed
by the same person meanwhile go straight to the shell. The shell uses plain pipes: ordinary commands,
scripts and line-based REPLs work; full-screen programs (vim, top) and password prompts (sudo) do not.
A line is never sent twice: if the bot restarts mid-run, it says so instead. Shells end after
`idle_minutes` without input (closed when a shell is next started) and when the bot stops. A routine you type (`$routine every 1h $t df -h`)
runs `$t` on a schedule; one a model set up asks for your approval each time.

Settings (`extensions/terminal/config.yml`): `shell` (a command line as a list, e.g. `[cmd.exe, /Q]`; on
Linux use an interactive shell, or `^C` ends it), `cwd` (home), `quiet_s` (1.5), `max_wait_s` (20), `update_s` (3),
`idle_minutes` (60), `tail_chars` (1800).

### The included host module

[`extensions/host`](../../extensions/host/extension.py) is this computer's desktop and files, for the
creator only, on Windows, macOS and Linux. Every capability is creator-only. Looking (screenshot, a
file, the clipboard, the process list) is a `private` read: it runs without asking, but only in the
creator's own private chats, and never for a secret file. Saving attachments onto the computer is
critical (a model's call asks); opening something and setting the clipboard are writes (asks by
default). The creator's own `$command` is their approval. Nobody else sees the commands in their menus.

| Capability | Command | Does |
| --- | --- | --- |
| `host.screenshot` | `$screenshot` | The whole screen, shrunk to a JPEG if over the upload limit |
| `host.get_file` | `$getfile <path>` | Sends a copy of a file (path absolute, or relative to home) |
| `host.save_files` | `$savefile [folder]` + attachment | Saves the creator's own attachments, only the ones named (`names`, so the approval shows them; default all of the creator's), to `~/Downloads` by default; existing names get ` (1)`. Files others added to the run are never saved |
| `host.read_clipboard` / `host.write_clipboard` | `$clipboard` / `$copy <text>` | The clipboard's text |
| `host.open` | `$open <url or path>` | Opens an http(s) or `mailto:` URL in the browser, or a file or folder with its default app |
| `host.processes` (read) | `$processes [cpu\|memory]` | The busiest processes |

Linux uses `grim` (wlroots Wayland, e.g. the Raspberry Pi), `gnome-screenshot` or `spectacle` (GNOME or KDE
Wayland) or `scrot` (X11) for screenshots, never X11 capture under Wayland (it would be black), `wl-clipboard`, `xclip` or `xsel` for the
clipboard and `xdg-open`; a bot running as a service finds the logged-in desktop session by itself
(`desktop_env` overrides it). Windows and macOS need nothing extra. Settings
(`extensions/host/config.yml`): `downloads_dir`, `desktop_env`. Shell work (listing folders, ending
processes) is the terminal's.

## Storing data

Pick the tier by what the data is; none of them needs anything new from the core:

| Data | Where | Example |
| --- | --- | --- |
| What was said, and what is remembered about it | The core's: `session.history` (recent, search, the rolling summary) and diaries. The core enforces their rules and people control them with `$memory` | A chat's summary; a character's notes about you |
| Small state of your module | `session.store(...)` / `context.store(...)`: key-value, JSON values, scoped by any combination of `conversation`, `persona` and `person`; your module's name comes first, so no module reads another's | A rotation counter (`conversation=True`), a person's preferences (`person=True`), a character's own data (`persona=...`) |
| What pages should show about a persona | Facts (`context.facts`, [above](#facts)): small public JSON values, read by interfaces only | A bond, a mood, the name someone calls her |
| Bulk or structured data of your module | Your own files or database in `context.data_dir`; the core never opens it | A trade log, price series, a transcript |

A "person" is a sender on one interface: the same human on Discord and on the web UI is two people to
the store.

## Core capabilities

Changing policy (`set_rule`, `set_route`, `set_forward`, `set_routine`, `set_assignment`,
`delete_assignment`) is critical and `never_auto`: a model's call always asks. `core.send_message` is
critical and grantable per `address`; the `list_*` reads of policy are `private`. An owner's `$command`
counts as approval.

| Capability | Parameters |
| --- | --- |
| `core.send_message` (critical) | `address`, `text`. An explicit owner send: not subject to admission. Returns `data["message_id"]`; fails with `delivery_failed` or `delivery_uncertain`. `owner_only` is judged by the run's trigger sender. |
| `core.list_rules` / `core.set_rule` | `pattern`, `rule` (object or null to delete) |
| `core.list_routes` / `core.set_route` | `pattern`, `route` (`{target, kinds}` or null) |
| `core.add_routine` (write, anyone) | `schedule` (`every 1h`, `daily 08:00`, `weekdays 09:00`, `mon,wed 18:30`, `at 18:30`, `at 2026-10-17 08:00`), `text` (a message or a `$command`), `chat` (admins: another conversation) |
| `core.list_routines` (anyone) | none: this conversation's routines; every conversation's for admins |
| `core.search_history` (read, anyone) | `query`, `limit` (1–50): this conversation's messages from before the request that called it, containing it, with time and speaker. Matching ignores case and how a letter is written (é as one character or two, full-width letters, İ and i). |
| `core.pause_routine` / `core.resume_routine` / `core.delete_routine` (write, anyone) | `id`: the caller's own routine here; admins: any |
| `core.set_routine` (owner) | a configured routine in `routine:<name>`: `name`, `text`, `every_minutes` or `daily_at`, `enabled` |
| `core.list_assignments` / `core.set_assignment` / `core.delete_assignment` | `pattern`, `assignment` |
| `core.cancel_runs` | `address` |
| `core.list_forwards` / `core.set_forward` | `address`, `target` (exact addresses; null removes) |
| `<interface>.<operation>` | as declared |

## Retention

Request IDs for deduplication and event receipts for owner relays are kept for 30 days. Run
workspaces are deleted at startup 7 days after their run ended; copy files elsewhere if they must
live longer. Conversation history, runs and approvals are kept indefinitely.

## Routine examples

In any conversation, `$routine every 1h $status` posts `$status` there every hour as you, and
`$routine weekdays 09:00 what is on my reminders today?` asks the conversation's engine each weekday
morning. People can also just ask the bot ("check the server every hour"), which calls
`core.add_routine`; those run the same way, as the person who asked, with one difference: a routine a
model set up does not speak for its creator. Its `$commands` are not the creator's approval (writes
wait for approval), it cannot use `$approve`, `$deny`, `$admin`, `$call`, `$setup`, `$permissions`,
`$memory clear` or `$memory forget` (forgetting is the person's own decision), and routines it adds are
marked the same way. `$routine` lists them,
`$routine pause|resume|delete <id>` manages them.

For unattended system work that reports elsewhere, the owner creates a configured routine:
`core.set_routine(name="medicine", daily_at="08:00", text="Remind everyone to take their medicine.")` and
`core.set_route(pattern="routine:medicine", route={"target": "wechat:family@chatroom"})`. The run happens
in `routine:medicine` under the `routine:medicine` (or `routine:*`) assignment; its replies go to the
target only while that conversation is admitted.

## Testing

There is no test package in `contracts`; copy the pattern. Load your extension with a real core in a temporary directory: a small `core.yml` listing your
extension plus a test interface whose `deliver` records replies and which calls `port.submit`
directly. See [`tests/core_support.py`](../../tests/core_support.py) and
[`tests/core_fixtures`](../../tests/core_fixtures) for working examples, and
[`tests/test_core_boundaries.py`](../../tests/test_core_boundaries.py) for the import rules.
