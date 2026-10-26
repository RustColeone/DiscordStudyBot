# The lab, specified

What the lab does, precisely enough to reimplement it in another language without reading the Python. The
README says how to use it; this says how it behaves. Machine-readable parts live next to it:

| Part | Source of truth |
| --- | --- |
| Core types and interfaces | [`docs/spec/contracts.json`](../../docs/spec/contracts.json) (generated from `contracts/`) |
| The module's surface: capabilities, settings, workflows, schemas, prompts, narration lines, the allowlist, the board's tables | [`docs/spec/workbench.json`](../../docs/spec/workbench.json) (generated, `tools/describe_module.py`) |
| The frontend protocol | [`PROTOCOL.md`](PROTOCOL.md) |
| Behaviour, end to end | scripted scenarios in [`tests/test_workbench.py`](../../tests/test_workbench.py) |

## 1. Task states

| State | Meaning | Left by |
| --- | --- | --- |
| `invited` | An invitation from another conversation; nothing started | accept → `queued` at once (a second accept is refused), then `working`; discard → `discarded` |
| `queued` | Created by a frontend, its first request not yet taken | the engine's first round → `working` |
| `working` | A round is running | the round's end (below); cancellation → `cancelled`; an error → `failed` |
| `review` | One change set ready; the second check waits (or the final approval, with `second_approval: false`) | second check approved → `approved`; a message → another round (`working`); `$cancel` → `cancelled` (the waiting run hears it through `Engine.stopped`); discard |
| `approved` | Checked; the final approval waits | landed → `landed`; refused → `review`; a message → another round; `$cancel` → `cancelled` |
| `answered` | A question answered, or nothing needed changing (a question about a change set that waits for you leaves it `review`/`approved`, below) | a message → another round |
| `failed` | The round did not produce a change that passes; nothing applied (the worktree stays) | a message → another round; discard |
| `cancelled` | The run was stopped | a message → another round; discard |
| `landed` | One commit on the project's branch (closed) | — |
| `discarded` | Worktree and branch removed (closed) | — |

A conversation has at most one open task (`invited`…`cancelled`); a message in a conversation goes to its open
task as another round, or starts a new task. A message that arrives while the task is `working` is queued and taken
by that run once its current round ends (one more round, on the same change set); once the run presents its outcome
(any other status) a message starts a round of its own, which takes the task over. Every way of presenting changes
the status before anything else can run, so no message falls between the two.

A round that answers a question keeps a change set standing: one this run made in an earlier round, or one that
waited for the second check or the final approval before the question. After the answer it is asked for again,
with the same checklist (ticks are kept per change set) or, once checked, only the final approval. A request of the
replaced run is closed; deciding it changes nothing ("a newer message took this task over").

## 2. A round

```
round(task, number, text, followup):
    status := working
    signals := router(text)                    # kind question|change, size tiny..large, risk low|high, title
                                               # (a cheap model; keywords if none answers)
    workflow := person's pick | routing table(signals)   # §5
    if kind = question and workflow = single:  return answer(text)   # read-only, no worktree
    prepare: open the worktree (first round), run the tests once ("baseline"), list the files,
             acceptance criteria := criteria(text, earlier criteria)  # §7
             brief := text + criteria + project instructions (AGENTS.md, CLAUDE.md) + where it came from + notebook
    outcome := workflows[workflow](round)      # §6
    outcome := verify(round, outcome)          # §7
    finish(outcome)                            # §8
```

## 3. Durable by replay

The engine is a pure function of its journal. After every restart and every approval the core runs it again from
the top; each step that costs or changes something is recorded under a **journal key** the first time and read back
afterwards. A replay therefore takes the same path and repeats nothing.

| Key | Records |
| --- | --- |
| `r{n}.signals`, `r{n}.route.<seat>` | the router's reading |
| `r{n}.requirements.m` | the criteria call |
| `{phase}.{turn}.m` | a solver turn's model answer (with `seat`: whose answer it was) |
| `{phase}.{turn}.a{i}` | the i-th tool call of that turn (its `call_id` too, so the core's own action log agrees) |
| `{phase}.{turn}.say`, `<key>.say` | that a line was said (said once, never twice) |
| `{phase}.seat` | who took a staffed role (a replay asks her first) |
| `{phase}.out.<seat>` | that this seat could not answer here (`{reason, stuck}`): a replay never asks her again |
| `r{n}.check.ask[.k]` | the k-th request of the second check (re-asked when items were unticked) |
| `r{n}.land.ask` | the final approval |
| `r{n}.check.denied`, `r{n}.land.denied` | that the denial was recorded in outcomes.jsonl (`….no` is that it was said) |
| `r{n}.queued` | the feedback taken after round n (none: the run presents) |
| `r{n}.probe[.k].…`, `r{n}.repair{k}.…` | the tester's k-th look and the k-th repair |
| `r{n}.repair{k}.undo` | putting the worktree back to the tested change after a repair that broke it |

Rules: a key is derived only from the round number, the phase and counters that themselves come from journaled
results (turn numbers, attempt numbers); never from time or randomness. Every decision a replay could take
differently is journaled: answers, tool results, and failures too (a seat that could not answer, `….out.<seat>`),
so a provider that recovers between two passes changes nothing. Writes to the board (status, steps) are
idempotent and may repeat on replay; model calls, tool calls with effects and lines said may not. A tool call
waiting for approval suspends the run (`WAIT_APPROVAL`); the decision arrives as an event and the replay continues
past it. When the core stops, a run's work is cancelled with `contracts.SHUTDOWN`: the run stays open and is
replayed on the next start, so the engine records nothing (only another cancellation is `cancelled`). When code changes between two replays of one run, keys can point at answers from the old path: finish
such a run, or start a new round.

## 4. Seats and staffing

A **seat** is a classmate: a model family, `cheap` and `strong` model lists (her own profile per tier, no silent
fallback to another family), an avatar, and `also` (classroom personas she stands for). A **role** is an ordered
list of seats: `router`, `drafter`, `judge`, `strong`, `critic`, `tester` (cheapest first).

`lineup(role, avoid_family, not_seat)`: the role's list (the task's crew first, in list order), then its sibling
role's list (strong ↔ drafter, judge ↔ critic), then every other seat; seats without a key left out unless nobody
has one; ordered by (another family than `avoid_family` first, `not_seat` last, list rank). For `judge` and
`critic`, `not_seat` (the drafter) is removed entirely: never her own reviewer.

`staffed(role, work)`: try `work(seat)` with each seat of the lineup until one finishes. A seat that cannot answer
(no key, its provider down, its answer unreadable twice, cut off twice) is replaced, and the narrator says so once
(unless the work is background, such as writing the criteria); the step she was on is closed `failed`. Nobody left:
the round stops (`Stopped`). Work given to a named seat (the fixer, the reviser) goes to the lineup the same way if
she cannot go on, without asking her again.

An answer cut off at the length limit is asked again once with twice the room (`max_tokens_cap`) and low effort,
with an instruction to do less per turn; any other unreadable answer is asked again once with the error.

## 5. Routing

| Signals | Default workflow | `routing:` key |
| --- | --- | --- |
| question | single (read-only) | `question` |
| change, high risk | critique | `risky` |
| change, tiny | single | `tiny` |
| change, small | cascade | `small` |
| change, medium / large | critique | `medium` / `large` |

`routing:` may point any key at any registered workflow. The person's pick (per task) wins.

## 6. Workflows

A workflow is `run(round) → Outcome`, registered by name (`workflows/`). Built in:

- **single**: the drafter does it (`solve`), then the tests and the change set.
- **cascade**: a cheap draft; if it is unfinished or its tests fail, or the **judge** (another family, not the
  drafter) asks for changes, the **strong** seat takes over with the draft, the reason and the judge's notes.
- **critique**: a draft (by the strong seat for a large or risky change); the **critic** (another family, never
  the drafter) reviews; unless she approves and the tests pass, the drafter revises once with her review.
- **plan**: the **planner** (role `planner`, strong models, read-only tools) studies the task and answers with a
  plan: the files and exact signatures, every rule and edge case with the exact values and messages, and how to
  check each part. The drafter carries it out (`execute` mode: the brief plus the plan; where the plan is wrong she
  does what the task needs and says so), then the tests and the change set. A planner who cannot finish a plan
  leaves the drafter the plain task. Never routed to by default; chosen per task, or `routing: {large: plan}`.

`Outcome`: kind (change | answer), ok, reason, summary, change set, test result, verdict, who finished, who
drafted, warning, verification, unreviewed. An outcome is **ok** when the work finished, the tests (if any) pass,
and something changed.

**solve** (a classmate with tools): turn by turn, each turn one JSON object `{say, note, plan, actions, status,
summary}` (nothing required; `status` defaults to continue with actions, done with an empty list, and continue
without the field, except on the last turn). Up to 6 actions run in
order; a failed one skips the rest; the results come back as the next message. A turn that says continue with no
actions is told so and counts as a turn. A working solver's first finish while the diff is empty is questioned once
(nothing changed: make the change, or finish again and say why); finishing again stands. A seat with
`tool_calls: true` works through her API's own function calls instead (contracts `ModelRequest.tools`): the tools as
functions (arguments described, none required; a tool missing one says so in its result), plus `plan` and `finish`
(status done or blocked, summary, say, the tester's findings); her words are the turn's note, the other calls its
actions. A finish beside other calls stands once they all ran without failing. Her history is her own messages
(calls with their `replay`, one tool message per call, the club's word as a user message); a classmate's turns
before a takeover come as one note. Ends on done or blocked, or after the turn limit with one last turn
without tools. Older exchanges are shortened.

**Reviews** (judge, critic): no tools; they get the task, the criteria, the project's instructions, the diff and the
test output; they return `{verdict, say, summary, requirements: [{id, met, evidence}], unexplained: [{file, change}],
issues: [{severity, category, file, line, problem, suggestion}]}`. The verdict is computed, not taken: it is
**revise** when there is a blocker or major issue, an issue of category correctness, requirement or scope at any
severity, a criterion not met, or an unexplained change.

## 7. Verification

**Criteria**: at the start of a round, the request (and the earlier criteria, for a follow-up) becomes up to 10
checkable criteria (`behavior`, `keep`, `tests`, `docs`, `other`; a follow-up adds at most 5), numbered `R1…`.

**verify(round, outcome)**, when the outcome is an ok change and probing is on (`probe: auto`: not a tiny change,
or any `keep` criterion):

```
findings := tester(outcome)                   # role tester: another family than who finished where one can
                                              # answer; within one family the tester line-up's order (cheapest)
repeat up to `repairs` (1) times while findings:
    fixer := who finished; she repairs with the findings (a solve, mode repair, on the tier she finished on);
             tests; change set
    if the repair is not ok (tests fail, unfinished, nobody could):
        restore the worktree to the tested change set (its git tree) and keep that outcome; stop
    findings := tester(outcome)
outcome.verification := {criteria, rounds: [{findings, probes, tester}], repairs, probes, findings}
findings left: a warning (saying whether the fix was tested again, or undone), and one checklist item each
```

The **tester** is a solve in probe mode: she may read, write probe inputs into `.probe/` (never part of a change
set), and **probe**: run one shell line in the task's worktree and, the same way, in a pristine checkout of the
base commit (`<task>.base`), with the same `.probe/` files, returning both outputs and whether they are the same.
Her last turn lists findings `{criterion, problem, evidence}`, each shown by a probe.

## 8. Presenting and the two approvals

An answer, or a change that changed nothing: status `answered`. A failed outcome: `failed`, with the reason;
nothing applied. Otherwise: status `review`, the warning (if any) said, and, with `second_approval`:

1. **The second check**: a checklist (each changed file, the tests, the review's notes, criteria met, the tester's
   result and each problem she still sees, commands that ran, the summary). The `review` capability is approved by
   the creator and succeeds only when every item is ticked for the current change set (`check` ticks them; ticks
   are kept per fingerprint, and a tick names the fingerprint it was made for). It is refused for a task that is
   not `review`/`approved` and when no checklist was asked for this change set. Approved with items unticked: "not
   yet", and a new request (up to 6).
2. **The final approval**: the `land` capability (critical, never automatic) makes the change set one commit on the
   project's current branch. The commit is made from exactly the reviewed files (a fresh index of the base commit
   plus the worktree's files, `commit-tree` with the base as parent), so what was staged or committed in the
   worktree meanwhile changes nothing. It goes on as a fast-forward when the branch has not moved, else a
   cherry-pick (after a dry-run merge where git can, 2.40 or later). It refuses a different fingerprint, an
   unchecked change set, uncommitted changes in the files it touches, staged changes, a detached HEAD, a conflict,
   and changes nothing then; the worktree is never changed, so the task can land later. `land: branch` leaves the
   commit on the task's own branch instead.

## 9. Visits and reports

A task started by an invitation from another conversation carries `invited {by, lead, crew, from, notes, excerpt}`
and `origin_address`. Its **lead** narrates; the **crew** take their roles first. **Report back** sends a report
(outcome, change, tests, who finished and reviewed, who was in the lab and who was asked along but not there, the
summary, a link) to `origin_address` as the creator's `core.send_message`; it can be sent again after a new outcome.
The lab as a place (`places.py`, contracts `PLACE_STATUS`): people on a visit (lead, crew, whoever worked on it)
are in the lab from the start of a task until it is reported back or closed, or `visit_minutes` after it last
moved; an invitation not followed yet is a ticket.

## 10. What a port needs from its platform

Everything else is the core's contracts and this module's own code. The platform services, in one place
(`workspace/system.py`):

| Service | POSIX | Windows |
| --- | --- | --- |
| Run one shell line (`run`, `probe`, tests) | `bash -c` | Git for Windows' bash |
| A process group that a timeout ends as a whole | new session, `killpg` | new process group, `taskkill /T` |
| A background process that outlives the terminal (`club` starting a lab) | new session | new process group, detached |
| Borrow a folder from the checkout in a worktree (`link`) | symlink | directory junction, else a copy |
| Is a process ours, and stop it (`club stop`) | psutil (else `/proc`) | psutil |
| Hand the process over to the core (`python -m extensions.workbench`) | `execve` | start it and wait |
| The locale commands run in (git's messages read the same everywhere) | `C.UTF-8` (macOS `en_US.UTF-8`) | the same |
| A project's own Python (`{python}`) | `venv/bin/python` | `venv\Scripts\python.exe` |
| git | the `git` program; a dry-run merge before a cherry-pick from 2.40 | the same |

Files are written as given (no newline translation) and an edit of a CRLF file keeps its line endings; paths are
compared with `/` and `.git` in any case is off limits.
