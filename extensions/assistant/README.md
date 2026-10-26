# `assistant`: the single-persona engine and reference implementation

The assistant is an engine in which one persona talks with people, uses tools, waits for approvals and
asks questions. Every step is saved, so a run survives restarts without repeating work. The core's
default configuration assigns it to every conversation and every routine, with the core's `assistant`
persona.

Use it for any conversation that needs one voice. Study it before writing your own engine: it is the
smallest complete example of the engine contract (state, events, tool calls, approvals, questions,
images, diaries).

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Engine | `assistant` | Persona mode `one`. "One persona that talks, uses tools and asks questions; resumable." |
| Setting | `max_steps` | `number`, default 8: "Most tool rounds per request" |
| Setting | `history_limit` | `number`, default 16: "Earlier messages the assistant reads" |
| Setting | `effort` | `text`, default `low`: "Reasoning effort: low, medium or high" |

The module contributes no personas, providers or profiles. The default `assistant` persona (display
name `Assistant`, profile `default`) and the `default` profile are defined by the core in
[`core/defaults.py`](../../core/defaults.py). Any persona works with this engine, including the room's
characters.

## Setup

The module loads by default. Its settings live in `extensions/assistant/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml):

```yaml
max_steps: 8
history_limit: 16
effort: low
```

| Key | Default | Meaning |
| --- | --- | --- |
| `max_steps` | 8 | Model turns per run. When they run out the model is asked for a closing message. |
| `history_limit` | 16 | Earlier conversation messages the model reads. |
| `effort` | `low` | `effort` of every model call (`low`, `medium` or `high`). |

`extension_config.assistant` in `core.yml` is merged over this file. The module needs no environment
variables of its own; the persona's profile decides which provider keys matter (for the `default`
profile, any of `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_API_KEY`).

Per-conversation behaviour belongs in the assignment in `core.yml`:

```yaml
assignments:
  "*":
    engine: assistant
    personas: [assistant]          # exactly one
    options:
      instructions: "Keep replies short."
      owner_instructions: "Call them captain."   # only when an owner speaks
      history_scope: sender
```

| Option | Meaning |
| --- | --- |
| `instructions` | Added to the system prompt. |
| `owner_instructions` | Added only when the request's sender is an owner. |
| `max_steps`, `history_limit` | Override the module settings for this conversation. |
| `history_image_minutes` | How far back (default 30) images people sent are shown with the request. |
| `history_scope: sender` | In group conversations, read only this sender's messages and the replies to them, and only their other open requests. |
| `quote` | `latest` (default) quotes the newest message of the run; `trigger` keeps quoting the original request. Only in group conversations whose interface supports quotes. |
| `diary` | `false` turns the persona's diary off. |

To stop it answering in a conversation, assign another engine there or set `enabled: false`. To remove
the module, add it to `modules: {exclude: [assistant]}`, and first replace the default `*` and
`routine:*` assignments, which name it; `python -m core --check` reports any assignment whose engine
is not registered.

## Using it

* The persona answers every admitted request. In group conversations each message in its context is
  prefixed with the sender's name, and replies quote the newest request of the run where the interface
  supports quotes.
* Before running tools it says briefly what it is about to do, then reports from the results. It is told
  never to announce an outcome before the `[tool]` result arrives. Files from tools are delivered
  automatically.
* A write that needs approval waits: the core asks the owner, who decides with `$approve` or `$deny`
  (see [Approvals](../../docs/core/ARCHITECTURE.md#approvals)), and the same run continues with the
  result or the denial.
* It may ask one question; the next message in the lane answers it and the same run continues.
* `$model <persona> <provider>/<model>` (admins) picks the persona's model for this conversation.
* While another request is still running in the conversation (for example a long conversion), the
  persona is told about it and says it is in progress instead of starting it again.
* Limits: at most 5 actions and 2 diary notes per turn, `max_steps` turns per run. Model failures fail
  the run, and the interface reports it.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION` with its settings; `setup` builds `AssistantEngine` from `config.yml`. |
| [`engine.py`](engine.py) | `AssistantEngine` and its helpers for images and history. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

### The turn loop

`run(session)` is called when a run starts and again after an answer, an approval or a restart.

1. **State.** `session.state`, or a fresh `{transcript, pending, event_cursor, steps, call_seq, quote}`.
2. **Events** (`_absorb_events`). Every event after `event_cursor` is applied once:
   * `input`: the request (the trigger is the first `input` event) is added to the transcript, and its
     images are kept.
   * `tool_result`: matched to a pending action by `call_key` (`"call:" + call_id`), noted in the
     transcript; a failed result drops the rest of the batch; files are delivered.
   * `approval_denied`: the batch is dropped and the transcript says the owner denied it.
   * `resumed` needs nothing: pending actions are simply run again with their saved `call_id`s.

   Then any pending actions are executed (`_execute_pending`).
3. **Inbox.** Messages that arrived while the run was thinking (`session.inbox()`) are added.
4. **Step limit.** At `max_steps` the engine asks for one closing turn (`_close`), speaks its message
   if there is one, and returns `RunYield.DONE`.
5. **Turn** (`_turn`). One model call; diary notes are kept; the turn is appended to the transcript;
   each action becomes a pending entry with `call_id` `c1`, `c2`, ...; then `session.save(state)`.
6. **Speak.** The message is sent with `session.reply` after the save.
7. **Act.** Pending actions run in order through `session.tools.call(..., call_id=...)`. A
   `needs_approval` result saves the action as `awaiting` and returns `RunYield.WAIT_APPROVAL`. A
   failure drops the rest. Successful files are sent with `session.send_file` and their delivery is
   reported back as `[tool files] ...`. Then the loop goes back to step 3.
8. **End.** A turn without actions returns `RunYield.WAIT_INPUT` for `needs_input`, otherwise
   `RunYield.DONE`.

### The turn format

`TURN_SCHEMA` is passed as `output_schema` (schema name `turn`), so `result.data` is validated JSON:

```json
{"message": "...", "status": "continue|complete|needs_input|blocked",
 "actions": [{"tool": "basic.now", "arguments": {"timezone": "UTC"}}],
 "diary": [{"key": "study.subject", "kind": "fact|episode|reflection", "note": "...", "importance": 3}]}
```

`_problem` adds rules the schema cannot express: only listed tools; `continue` needs at least one
action; other statuses take none; `needs_input` and `blocked` need a message. An invalid or rejected
turn is sent back with the problem as a correction, up to two times; then the `ModelError` fails the
run.

### Function calls

Where the model can call tools itself (an OpenAI-compatible provider: DeepSeek, OpenAI), a turn is its
words and its function calls instead of JSON (`tool_calls`, on by default; `tool_calls: false` in the
engine's settings, or the conversation's `tool_calls: false` option, keeps JSON turns):

* the words are `message`, the calls `actions` (at most 5), and the turn is `continue` with calls,
  `complete` without;
* the engine adds two tools of its own: `ask` (one non-blank `question`, called alone: `needs_input`; the
  question is added to the message unless the words already ask one) and, with the diary on, `remember`
  (one diary note, at most 2 per step, which goes with an answer: a step of notes and no words is sent
  back);
* each call gets an id unique in the run (a server's own ids may repeat), and each action keeps it,
  so its result goes back as that call's `tool` message; a refused call and the calls dropped after it
  are answered too (the APIs refuse a call left unanswered), and the core places every answer right
  after its call. The note that a call waits for the owner stays in the JSON form only: in calls, its
  result says what came of it;
* the transcript keeps both forms: each step as the JSON turn it amounts to, with its calls and
  `replay` beside it (and a JSON step reads as its words in a request with calls). So a step can always
  fall back: when the next candidate in the profile cannot call tools (`ModelError("tools_unsupported")`),
  or the model calls wrongly twice, that step is asked as JSON, where every candidate counts; a run saved
  with JSON turns goes on with calls.

The closing turn at `max_steps` is always JSON. The system prompt then says to call the tools as
functions instead of listing them (they go as `ModelRequest.tools`, with their risk in the
description), and the diary rules say to call `remember`.

### What the model sees

`_messages` builds the system prompt from the persona prompt, its display name, the current time,
`options.instructions`, `owner_instructions` (owners only), the turn contract with the display rules
(Markdown or plain text, `display.max_text_chars`) and the tool catalog (name, description,
parameters, risk), the diary, and up to 5 other open runs from `session.activity()`. Then come the
history (`history_limit` messages before the request; the bot's replies as `assistant`, people's
messages as lines with time, speaker, quoted text and attachments) and the run's transcript.
Consecutive messages with the same role are merged, and the first message after the system prompt is
always a `user` message.

The request is a plain `ModelRequest` without `profile` or `persona`: with one persona, the core uses
her profile and her `$model` choice.

Images (latest 6) come from the run's messages, successful tool results, and images people sent within
`history_image_minutes` before the request (up to 4; WeChat sends a picture and its question
separately). They are passed as `images` on every turn, and history lines say which attachments are
shown.

### Design decisions

* **Speak, then act.** The message only announces; results come back as `[tool ...]` notes the next
  turn reads.
* **No fixed text.** Even at the step limit the model writes the closing message; if that fails,
  nothing is said.
* **Stop at the first failure** in a batch, so later actions never run on a failed prerequisite.

### Safety

The model can only call tools that `session.tools.list()` returns, and the core applies permissions
to every call. A write never runs on the model's say: it comes back as `needs_approval`, the run waits
durably, and only the owner's decision resumes it. Diary notes are data, not instructions, and the core
refuses notes that copy the message or exceed its limits.

## Data it keeps

* **Run state**: `transcript` (the run's turns, inputs and tool notes), `pending` actions, `event_cursor`,
  `steps`, `call_seq`, `quote` and `images` (up to 6 file references). Runs are kept indefinitely; run
  workspaces are deleted 7 days after the run ends
  ([Retention](../../docs/core/EXTENSIONS.md#retention)).
* **Diary**: the persona's notes about the sender in this conversation, recalled by the request's
  words (up to 12 notes, about 3500 characters). Only importance 3 or more is written; the core keeps
  at most 200 notes per persona and person ([Diaries](../../docs/core/EXTENSIONS.md#diaries)).

## Tests

[`tests/test_core_assistant_engine.py`](../../tests/test_core_assistant_engine.py) runs the engine in a
real core with a scripted model. It checks speak-act-answer; a write that waits for approval across a
restart; images from attachments, from a picture sent just before the question and from tool results;
owner instructions only for owners; corrections for invalid output and unknown tools; a question
answered in the same run; files delivered once; group quoting and speaker prefixes; `history_scope`
and `instructions`; other open runs shown to the model; `quote: trigger`; the closing message at the
step limit; and a run failure when no model is available.
[`tests/test_core_diary.py`](../../tests/test_core_diary.py) covers diaries through this engine. Many
other core tests load it as their engine ([`tests/core_support.py`](../../tests/core_support.py)).

```
venv/bin/python -m unittest tests.test_core_assistant_engine tests.test_core_diary
```

## Building something similar

* Read events by index and keep `event_cursor` in state ([`engine.py:164`](engine.py)).
* Give each action its `call_id` and save before calling, so a replay returns the stored result
  ([`engine.py:139`](engine.py), [`engine.py:199`](engine.py)).
* Save before speaking ([`engine.py:143`](engine.py)).
* On `needs_approval`, save and return `WAIT_APPROVAL`; handle the `tool_result` or `approval_denied`
  event on the next call ([`engine.py:203`](engine.py), [`engine.py:175`](engine.py)).
* Validate the parsed turn yourself and send problems back as corrections
  ([`engine.py:338`](engine.py), [`engine.py:358`](engine.py)).
* Use `session.activity()` so parallel runs do not repeat each other ([`engine.py:325`](engine.py)).
* Pitfalls: `EngineSpec(..., "one")` needs exactly one persona in the assignment; exceptions fail the
  run, so catch only what you can recover from; diary writes may raise `ValueError` and must not break
  the reply ([`engine.py:295`](engine.py)).

See also: [Writing modules](../../docs/core/EXTENSIONS.md) (especially
[Engines](../../docs/core/EXTENSIONS.md#engines) and
[The built-in `assistant` engine](../../docs/core/EXTENSIONS.md#the-built-in-assistant-engine)),
[Core architecture](../../docs/core/ARCHITECTURE.md#durable-runs), [`extensions/room`](../room) (the
multi-persona engine) and [`plugins/whale_chan`](../../plugins/whale_chan) (another engine).
