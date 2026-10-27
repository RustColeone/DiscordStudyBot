# `room`: the shared room, where several characters work one request together

The room is an engine in which several personas share every request in a conversation. Each character
speaks in her own voice, uses tools and hands focused jobs to the others on a shared work board. Jobs
that do not depend on each other run in parallel. Its usual characters are the students (Luna, Gemini, Claude,
DeepSeek and Grok), who live in their own module, [`extensions/students`](../students), with their prompts and
models. The room only runs them.

Use it when you want a group chat with several characters instead of one assistant. Study it if your
engine has more than one persona (`EngineSpec(..., "many")`), runs model turns concurrently, or must
keep a work queue across approvals, questions and restarts. For the one-persona version of the same
contract, read [`extensions/assistant`](../assistant) first; it is shorter.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Engine | `room` | Persona mode `many`. "Several personas share one work board: they talk, use tools and hand each other jobs, in parallel where independent." |
| Setting | `max_steps` | `number`, default 24: "Most work-board steps per request" |
| Setting | `history_limit` | `number`, default 30: "Earlier messages every character reads word for word" |
| Setting | `chime_in` | `boolean`, default false: "A group message naming no one: whose turn it is answers, the others may join in" |
| Setting | `compact` | `boolean`, default true: "Fold messages older than history_limit into the chat's rolling summary" |

The characters, their prompts, their model profiles and Grok's provider are the students module's: see
[`extensions/students`](../students). To give a character more (a personality of your own, a running joke, house
rules), give her prompt parts in core.yml ([Prompt parts](../../docs/core/EXTENSIONS.md#prompt-parts)).

## Setup

The module loads by default. Its settings live in `extensions/room/config.yml` (git-ignored); copy
[`config.example.yml`](config.example.yml) and keep only what you change:

```yaml
max_steps: 24
history_limit: 30
chime_in: false
compact: true
compact_every: 20
compact_profile: default
max_tokens: 1500
aliases: {claude: [Haiku]}      # other names people call a persona by
```

| Key | Default | Meaning |
| --- | --- | --- |
| `max_steps` | 24 | Model turns the whole room may take per run, across all characters. Clamped to 4..50, and never below the number of characters (so "everyone" lets each answer once). It also caps the jobs a character may add to the board. |
| `history_limit` | 30 | Earlier conversation messages each character reads word for word (at most 150). Older ones are in the rolling summary, and `core.search_history` finds their exact words. |
| `chime_in` | false | A message naming no one goes to whoever's turn it is, and the other characters may each add something or pass. Off: only the one in turn answers. |
| `compact` | true | Keep the conversation's rolling summary of the messages older than `history_limit`. |
| `compact_every` | 20 | How many messages must have scrolled out of the window before the summary is rewritten (one model call). |
| `compact_profile` | `default` | The model profile that writes the summary. |
| `max_tokens` | 1500 | `max_tokens` of each model turn. |
| `aliases` | `{claude: [Haiku]}` | Extra names per persona that count as addressing her. |

On/off settings accept `true`/`false`, `yes`/`no`, `on`/`off` and `1`/`0`, so `chime_in: "false"` is off. A
number that is not a number (`history_limit: abc`) falls back to its default instead of failing the run; the
same goes for these keys in an assignment's `options`.

The characters' `providers:` and `profiles:` used to be set here; they are the students module's settings now
(see [Moved from the room](../students/README.md#setup)). If this file still has them, the room ignores them and
records `settings_moved` in the audit log.

Assign the engine to a conversation in `core.yml`. The persona order is also the rotation order:

```yaml
assignments:
  "discord:234567890123456789":
    engine: room
    personas: [luna, gemini, claude, deepseek, grok]
    budget: {model_calls: 40, tool_calls: 24, seconds: 900}   # a busy room makes many model calls
    options: {instructions: "Help with the physics homework."}
```

Assignment `options` the room reads: `instructions` (added to every character's system prompt),
`owner_instructions` (added only when an owner sent the request), `max_steps`, `history_limit`,
`chime_in`, `compact`, `compact_every`, `compact_profile`, `history_image_minutes` (default 30) and
`diary: false`. The core reads `diary_scope` (a name: each character also reads her notes about a person
from every conversation whose assignment uses the same name; notes stay filed where they were written, so
the setting can change freely; see [Diaries](../../docs/core/EXTENSIONS.md#diaries)). The room does not read the assistant's `history_scope`
or `quote`.

Discord channels and the web UI use this same engine; only these options differ. For example, a quiet
Discord study channel and a lively web chat:

```yaml
assignments:
  "discord:234567890123456789": {engine: room, personas: [luna, claude], options: {history_limit: 20}}
  "webui:*": {engine: room, personas: [luna, gemini, claude, deepseek, grok],
              options: {history_limit: 40, chime_in: true, diary_scope: webui}}
```

To turn it off for one conversation, assign that conversation another engine or set `enabled: false`.
To turn the module off entirely, add it to `modules: {exclude: [room]}` in `core.yml`; the students stay, for
other engines to use, and `python -m core --check` reports any assignment still naming the `room` engine.

## Using it

* **Who answers.** Characters the message calls on answer: named at its start ("Luna, ..." or "Hey Luna and
  Gemini: ..."), at its end ("..., Luna?") or with `@` (by name, display name or alias, case-insensitive,
  as a whole word, ignoring text in quotes or code blocks). Someone named only in passing ("Luna, what did
  DeepSeek and I do?") is *mentioned*: once the answer is out she may add her own word, as her choice, or
  stay silent. A message that names people without calling on anyone ("Can DeepSeek check this?") is for
  all of them. "大家", "你们", "各位", "everyone", "everybody", "you all", "you guys", "all of you" and "the
  team" address everybody, unless a Chinese phrase says they need not ("你们不用...", "不用...大家"). An
  unaddressed message goes to one character in rotation per conversation. With `chime_in` on, the others
  may also add something short or stay silent (`pass`) after reading her answer; the one in turn always
  answers, so nobody is left without a reply. A chime-in (or a mentioned character's word) is one short
  remark: no tools, questions or hand-offs, and if it fails (or the run's steps are used up) she just stays
  silent.
* **Long conversations.** Each character reads the last `history_limit` messages word for word. Older
  ones are folded into the conversation's rolling summary, which every character reads, and she can
  look up their exact words with `core.search_history`. `$memory summary` shows the summary.
* **Replies.** Each character's message appears under her display name. In group conversations it
  quotes the request. The message is sent before her tools run, so people see what she is about to do.
  Her first message in a job always speaks, and so does her last; the working steps in between may be
  silent, so a three-step job does not say "good idea" three times (she sees what she already said as
  `your_messages`).
* **Work notes.** A character may add a short public reason for a step (at most 480 characters). It
  arrives before her message, labelled `Work note` or `工作说明`.
* **Teamwork.** A character can give teammates jobs. Their replies follow as they finish, in parallel
  where the jobs are independent.
* **Approvals.** A write waits for the owner's approval (where and how is the core's job: see
  [Approvals](../../docs/core/ARCHITECTURE.md#approvals)). Other characters keep working meanwhile.
* **Questions.** A character may ask the person one question. The next message in the lane answers it
  and every waiting job continues. Messages sent while the room is thinking join the run: they answer
  waiting questions, or open new jobs for the characters they address (or the next in rotation).
* **Models.** `$model` lists each character's model in the conversation; an admin can choose one per
  character with `$model <persona> <provider>/<model>` and undo it with `$model <persona> reset`. The
  core records on each reply which provider and model wrote it.
* **Diaries.** Each character keeps her own notes about each person; `$memory` shows and forgets them.
* **Limits.** At most 5 actions and 4 new jobs per turn, 2 diary notes per turn, `max_steps` turns per
  run. When a character cannot go on she says so with a fixed sentence, in Chinese if the latest message
  contains Chinese characters and in English otherwise; the others continue.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION` with its settings; `setup` builds `RoomEngine` from them. |
| [`engine.py`](engine.py) | `RoomEngine`: who answers, the board, scheduling, model turns, tool calls, diaries. |
| [`config.example.yml`](config.example.yml) | The documented settings. |

### The board

The run's state is a board of jobs. A job is
`{id, character, instruction, after, status, observations, pending, steps, question, said}`. Its status is
`pending`, `running`, `waiting_approval`, `needs_input`, `complete` or `blocked`. A chime-in job also
has `optional: true`; it waits (`after`) for the answering jobs, and starts once they have had their say
(complete, or waiting for an answer or an approval).

1. **Open** (`run`, `_compact`, `_open`). On the first call the engine first updates the rolling
   summary if enough messages have scrolled out of the window (see below), then records the trigger's
   text as the `goal`, keeps its images and adds one `opening-N` job per character who should answer
   (`participants`, or the next in rotation: the counter `rotation` in `session.store(conversation=True)`).
   Mentioned characters get optional jobs with the `MENTION` instruction; with `chime_in`, after an
   unaddressed message the others get optional jobs with the `CHIME` instruction.
2. **Absorb events** (`_absorb_events`). Events past `event_cursor` are applied: an `input` (other than
   the trigger itself) answers waiting jobs or opens `reply-N` jobs; a `tool_result` or
   `approval_denied` is matched to the job whose first pending action has that `call_id`, recorded as an
   observation, and the job goes back to `pending`. A denial is recorded as a failed result with error
   code `denied`, and the rest of that batch is dropped. Files of an approved result are queued in
   `state["deliver"]`. Any job left `running` (the process stopped mid-turn) is reset to `pending`.
   The `resumed` event needs no handling of its own.
3. **Schedule** (`_schedule`). A pending job starts as an `asyncio` task when every job in its `after`
   is `complete` and its character has no other running job. A job whose prerequisite is `blocked` is
   blocked too. An `asyncio.Event` wakes the scheduler whenever a turn adds jobs or a task ends, so new
   jobs start without waiting for the others.
4. **Work** (`_work`). Each turn costs one room step. The character's turn is checked, new jobs are
   added to the board, diary notes are written, and the message is spoken. `complete`, `needs_input` and
   `blocked` end the job. `continue` with actions runs them and takes another turn; `continue` with only
   jobs completes her job (delegating ends her part).
5. **Inbox** (`_take_inbox`). When the board settles, new messages from the run's inbox are added and
   the scheduler runs again.
6. **Yield.** After a final `save`: any job `waiting_approval` returns `RunYield.WAIT_APPROVAL`, else
   any `needs_input` returns `RunYield.WAIT_INPUT`, else `RunYield.DONE`.

### A model turn

`_turn` calls `session.models.generate` with:

```python
ModelRequest(messages, profile=persona.profile, persona=persona.name,
             output_schema=turn_schema(characters), schema_name="room_turn",
             images=..., effort="medium" if job["observations"] or job["after"] else "low",
             max_tokens=self.max_tokens)
```

`persona` matters: several characters run at once, so it tells the core whose `$model` choice applies
and whose model to name on the reply. The schema (`turn_schema`) is strict JSON:

```json
{"status": "continue|complete|needs_input|blocked|pass", "message": "...", "work_note": "...",
 "actions": [{"tool": "name", "arguments": {}}],
 "jobs": [{"id": "verify", "character": "claude", "instruction": "...", "after": ["opening-1"]}],
 "diary": [{"key": "study.subject", "kind": "fact|episode|reflection", "note": "...", "importance": 3}]}
```

`status`, `message`, `actions` and `jobs` are required; `character` is an enum of the session's
personas. `pass` (stay silent) is accepted only on an optional job, with no actions or jobs; it
completes the job without a reply. An optional job may otherwise only `complete`. `_check` then rejects a turn with an empty message, a `work_note` over 480 characters,
`continue` without actions or jobs, actions or jobs with any other status, unknown tools, `complete`
right after her own failed action, new jobs that would take the board past `max_steps` jobs (a turn adding
none is never refused for a full board), duplicate or overlong job
IDs, unknown characters, empty or overlong (over 2000 characters) instructions, and missing or cyclic
`after` dependencies. A rejected or unparsable turn gets one correction; a second failure blocks the job
with the `invalid_turn` sentence. Any other error in her turn (for example no model available) blocks
only her job (`request_failed`); the run goes on. An optional (chime-in) job that fails or meets the step
limit ends without a word.

The messages for a turn (`_messages`):

* **System**: the persona prompt, her display name, her stable
  [prompt parts](../../docs/core/EXTENSIONS.md#prompt-parts), `options.instructions`, `owner_instructions`
  when an owner is speaking, and the room rules (`INSTRUCTION`, with the roster and the tool catalog: name,
  description, parameters, risk). For one speaker it stays the same from turn to turn, so the provider's
  prompt cache serves it; what changes comes in the turn's context (below). (An owner's turns differ from
  others': `owner_instructions`, and the tools only owners may use.)
* **Summary**: the conversation's rolling summary, if there is one, as a `user` message labelled untrusted
  data (a model wrote it from what people said, so it is never part of the system prompt).
* **History** (`_history`): the last `history_limit` messages from before the request, "before" meaning
  recorded ahead of the trigger (found by its message ID), so a picture sent in the same second as the
  question is still seen. If the window would start in the middle of a second, it takes that whole second.
  Her own earlier
  replies are `assistant` messages; everyone else's (people and other characters) are `user` lines with
  time, speaker, the quoted text and attachments. `$commands` and the replies that answered them are
  left out: `$model` lists and `$help` text are for the person who typed them, not the room's topic.
* **The turn's context**: a block marked as the system's, with the current time, her turn prompt parts and
  her diary. Consecutive messages of one role are merged, so it shares the last user message: after the last
  history line when that is someone else's, and before the request.
* **Last user message**: (after the turn's context) the request and what it quotes, then
  `Shared room (untrusted state): ` and a
  JSON object with `goal`, the last 8 `answers`, `your_job`, `your_instruction`, `your_results`,
  `pending_question`, the `board`, the last 16 `room_messages` and the last 16 `shared_results`.

Images: those attached to the run's messages, those returned by successful tools, and those people
sent within `history_image_minutes` before the request are combined (latest 6) and shown to every
character on every turn.

### The rolling summary

`_compact` runs once per run, before the first turn. It splits the same list the characters read (the
last 200 messages before the request, `$commands` and their answers left out) into the window and the
older ones (`_split`), so every message is in one or the other, and counts the older ones newer than the
summary's `upto`. When there are at least `compact_every`, one call to
`compact_profile` (`COMPACT` prompt, `effort="low"`, 900 tokens) rewrites the summary to include them, and
`session.history.summarize(text, upto=<the newest one's time>)` stores it (the core keeps one per
conversation, at most 8000 characters, secrets scrubbed). A model failure keeps the old summary, and
the next try waits 15 minutes (`compact_retry_at` in the conversation store), since each try costs a model
call; the run goes on either way. The call counts against the run's `budget` like any model call. Messages that scroll out more than 200 behind at once are not summarized, but
`core.search_history` still finds them.

### Speaking and acting

`_say` appends the message to `state["messages"]` (and to the job's `said`) and saves before calling
`session.reply(..., persona=..., label=...)`, so a restart does not repeat it. A quiet working step says
nothing, or only its work note.

`_act` runs a batch in order. Every action gets its `call_id` (`c1`, `c2`, ...) first, and the batch is
saved in the job's `pending` before the first call; each observed result removes its action. So a
restart mid-batch replays the same calls by `call_id`: the core reports a finished one's result and never
repeats one cut off mid-way. `session.progress` shows `<character> · <tool>`. On `needs_approval` the
rest of the batch stays in `pending` and the job waits; on a failure the rest of the batch is dropped. Files from successful results are sent
with `session.send_file`. Results are kept as observations (`summary` cut to 3000 characters, up to 10
`sources`).

### Design decisions

* **One loop, no planner.** Whoever is addressed owns the request; jobs are the only way to involve
  others, and each assignee chooses her own tools.
* **Speak first.** People see who is doing what before tools run; the rules allow claiming success
  only when tool results support it.
* **Parallel but one job per character**, so a character never talks over herself and her history
  stays in order.
* **The board is untrusted data.** Tool results and teammates' words are marked as data, never
  instructions, in both the rules and the message label.
* **Guards on top of the core's.** `_repeated` compares each action's signature (tool and arguments)
  with earlier observations in the run: an action of risk other than `read` that already succeeded is
  not run again (the old summary is returned), and an identical failed action is retried once only if
  it was retryable (otherwise `repeated_failure`).
* **Fixed failure sentences.** Unlike the assistant, the room reports a blocked job with a short fixed
  text (`FAILURES`), because the other characters may still be working.

### Safety

Models may only call the tools `session.tools.list()` offers, by exact name; the core decides access
and approval for every call. Writes are proposed, not asked about: a call returning `needs_approval`
parks the job and the owner's `$approve` or `$deny` resumes the run. The owner's private instructions
are shown only when an owner sent the request. Diary notes are private to each character.

## Data it keeps

* **Run state** (saved with `session.save`, kept with the run): `goal`, `answers`, `jobs`, `messages`,
  `observations`, `steps`, `call_seq`, `job_seq`, `event_cursor`, `opened`, `images` (up to 6 file
  references) and, briefly, `deliver`. Runs are kept indefinitely; the run's workspace files are deleted
  7 days after it ends ([Retention](../../docs/core/EXTENSIONS.md#retention)).
* **Conversation store**: the rotation counter, `rotation`, and after a failed summary `compact_retry_at`,
  in `session.store(conversation=True)` (the module's store for this conversation).
* **Rolling summary**: the core's per-conversation summary (`session.history.summarize`); an admin can
  remove it with `$memory clear summary`.
* **Diaries**: each character's notes about the person who sent the request, filed in that conversation
  and read there, or also in every conversation of the same `diary_scope`.
  Only notes of importance 3 or more are written; the core enforces the rest (at most 200 notes per
  persona and person; see [Diaries](../../docs/core/EXTENSIONS.md#diaries)). Turn off with
  `diary: false`.

## Tests

[`tests/test_core_room.py`](../../tests/test_core_room.py) runs the room in a real core with a scripted
fake model per character. It checks rotation and naming (including the `Haiku` alias); that "大家"
starts one job per character in parallel and each reply names its own model; tools, work notes and
`after` dependencies on one board; a write that waits for approval across a restart and runs once; two
approvals resuming the run only when both are done; `$commands` and their answers left out of history;
chime-in (the one in turn answers, the others may pass after reading her answer, and a `pass` from the
one in turn is corrected); working steps that stay quiet while the first and last always speak; someone
mentioned in passing adding her word after the answer, or passing; the rotation kept per conversation across restarts; old messages folded into the summary
every character reads, only once `compact_every` have scrolled out; chime-ins limited to one quiet remark;
a full board still taking plain replies; settings written as strings; messages of the same second kept in
view; `$commands` never hiding older messages; a failed summary not retried on every message; a question
answered by the next message; one correction for invalid turns; a retryable failure retried
once only and unknown characters refused; images shown to every character; separate diaries; and the
profile precedence (code, then `config.yml`, then `core.yml`).

```
venv/bin/python -m unittest tests.test_core_room
```

## Building something similar

* **Declare persona mode `many`** ([`engine.py:198`](engine.py)) and pass `persona=` on every model
  call ([`engine.py:512`](engine.py)), or `$model` choices and model labels will be wrong.
* **Initialise state once and replay events by index** ([`engine.py:216`](engine.py),
  [`engine.py:294`](engine.py)). Skip the trigger's own `input` event if you already took it from
  `session.trigger`.
* **Save, then speak** ([`engine.py:425`](engine.py)); **give the whole batch its `call_id`s, keep it in
  the job, save, then call** ([`engine.py:438`](engine.py)), so a restart replays the same calls.
* **Keep the unfinished part of a batch** when a call needs approval, and match the later
  `tool_result` by `call_key` ([`engine.py:454`](engine.py), [`engine.py:306`](engine.py)).
* **Concurrency**: one task per job and an event to wake the scheduler; cancel and gather the tasks in
  `finally` ([`engine.py:353`](engine.py)). Catch `CancelledError` separately so cancellation is not
  reported as a failure ([`engine.py:409`](engine.py)).
* **Validate beyond the schema** and send the problem back as a correction
  ([`engine.py:528`](engine.py)), with a bounded number of retries.
* **Ship models with the module**: providers and profiles in `Contributions`, with the module's
  `config.yml` merged over them ([`extension.py:13`](extension.py), [`extension.py:40`](extension.py)).
* Pitfalls: a run returns only when no job can start, so a waiting job does not stop others; budgets
  count every character's calls, so give the assignment a larger `budget`; the room recognises a
  character's own history by display name, so two personas with the same display name would share it.

See also: [Writing modules](../../docs/core/EXTENSIONS.md) (especially
[Engines](../../docs/core/EXTENSIONS.md#engines) and
[The built-in `room` engine](../../docs/core/EXTENSIONS.md#the-built-in-room-engine)),
[Core architecture](../../docs/core/ARCHITECTURE.md#durable-runs), [`extensions/assistant`](../assistant),
[`plugins/webui`](../../plugins/webui) (a web chat that uses the room by default) and
[`plugins/whale_chan`](../../plugins/whale_chan) (another engine with its own persona).
