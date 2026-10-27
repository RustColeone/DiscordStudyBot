# The Classroom protocol

How a frontend talks to the Classroom: the web page, `club` and a window GUI all use the same HTTP and websocket API,
served by the Classroom's interface (`web/server.py`, port 4157 by default). The page is one frontend among others:
`web: false` turns it off and keeps this API; `api: false` turns the whole interface off (the Classroom is then reached
from Discord only).

The rules live in the Classroom, not in the frontends. The server says which approval a task waits for, with the
checklist and its ticks, and which actions are possible now (`views.py`). A frontend shows these; it never
decides them. The second check is enforced by the `workbench.review` capability, so every tick a frontend sends
must really be made.

## Access

The API answers only private-network clients. The `Host` must be an IP address, `localhost`, a local name
(`.local`, `.lan`, `.home.arpa`, `.internal`), this computer's name or one listed in `hostnames`; anything else is
refused (403), so a website cannot reach the Classroom through a name it controls. Requests from other sites are refused
too: the `Origin` header must match the address you called, or `public_url` when an HTTPS proxy stands in front.
Whoever connects acts as the Classroom's creator (the `user_id` setting, default `creator`), so keep the Classroom on
your own network.

## HTTP

| Request | Answer |
| --- | --- |
| `GET /api/state` | The whole state, as in the websocket's `hello` (below). |
| `GET /api/tasks/<id>` | `{"task": task view, "steps": [...], "messages": [...]}`: everything one task shows (a websocket client asks with `load` instead, so the snapshot stays in order with the events). |
| `GET /api/tasks/<id>/diff` | The change set: `{"files", "stat", "fingerprint", "patch", "live"}` (live from the worktree, or the patch kept once landed or discarded). |
| `GET /api/tasks/<id>/file?path=…` | One file as it is in the task's worktree (else the checkout): `{"path", "text"}`. |
| `GET /api/presence` | The Classroom as a place (`contracts.PLACE_STATUS`, below), as it publishes it. |
| `GET /api/prices` | Where prices come from and each classmate's model's price; `?refresh=1` fetches once a day old. |
| `GET /files/<id>/<name>` | A file the club sent (a diff file, say). |
| `GET /avatars/<key>` | A classmate's picture. |

## The websocket: `GET /ws`

The first message is `{"type": "hello", "state": {...}}`:

| Key | What |
| --- | --- |
| `title`, `user` | The Classroom's title; you (`{"name"}`). |
| `team` | Classmates: `persona`, `name`, `family`, `voice`, `avatar`, `cheap`, `strong`, `ready` (true or false: has a model key; null if unknown). |
| `roles` | Role → ordered seat names. |
| `projects` | `{name, path, test, test_line, link, land, source, detected}` each. |
| `tasks` | Task views (below), newest first. |
| `workflows` | `{name, title, text}` each: `auto` and every workflow in `workflows/`. |
| `places` | `{"here": this Classroom as a place, "others": [places other interfaces published]}`; `links`: interface → its link. |
| `progress` | Task → live "typing" lines. |
| `settings`, `prices`, `avatars`, `page` | The second-check and commands settings; the price list; picture keys; whether the page is on. |

### A task view

`{id, project, address, title, status, phase, workflow, created, updated, own, data, approval, actions}`:

- `status`: `invited`, `queued`, `working`, `review`, `approved`, `answered`, `failed`, `cancelled`, `landed`,
  `discarded`. `own`: the task belongs to this interface (you can reply to it here).
- `data`: `request`, `route`, `requirements` (the acceptance criteria: `{id, text, kind}`), `change`, `validation`,
  `review` (with per-criterion verdicts), `verification` (what the tester tried and found), `summary`, `totals`
  (calls, tokens, cost), `plan`, `branch`, `base`, `worktree`, `landed`, `failure`, `warning`, `invited`, `crew`,
  `origin_address`, `reporting` (a report on its way), `reported` (`{at, to, status}`, once it was delivered), and
  more.
- `approval`: the approval the task waits for now, or null: `{id, capability, kind, tier, summary, command,
  message}` (`message`: the approval card's message ID), and for the second check `{fingerprint, checklist: [{id,
  label, ticked}], remaining}`. It is never one decided elsewhere (typed, in Discord) or expired with its run (the
  server asks the core, contracts 1.7 `approval_status`), nor the second check or the final approval of a version
  the club is replacing (while it works).
- `actions`: what can be done now, any of `accept`, `send`, `check`, `approve`, `deny`, `report`, `discard`,
  `forget`.

### Events from the Classroom

| Type | Fields | When |
| --- | --- | --- |
| `task` | `task` (a view) | A task changed (status, approval, ticks...). |
| `step` | `step` | A step started or changed: `{task, id, kind, role, seat, status, title, data, started, ended}`; status `running`, `done`, `failed` or `skipped` (left open by a run that stopped: once a task is `failed`, `cancelled`, `landed` or `discarded`, none of its steps is `running`); kinds `route`, `requirements`, `turn` (a classmate's turn, `data.actions` her tool calls, `data.findings` for the tester), `validate` (tests), `change`, `review`, `approval`. |
| `message`, `message_update` | `item` | What the club says (`kind: text`, `speaker`), approval cards (`kind: notice`, `code: approval_request`, `data.approval_id`), files, your own messages (`mine`, `choice`). |
| `progress`, `progress_clear` | `task`, `run_id`, `text` | A live line ("Pro-chan · drafting…"). |
| `team`, `projects` | `team` / `items` | Who has a key; the projects. |
| `places` | `places`, `links` | Who is where changed (every few seconds at most). |
| `loaded` | `task` (a view), `steps`, `messages`; or `id`, `missing: true` | The answer to `load`. |
| `deleted` | `id` | A task was removed from the list (`forget`). |
| `folder` | `path` | The answer to `folder`: where a new project would be made. |
| `ack` | `id`, `ok`, `reason`; `task` for `start` | The answer to `start`, `accept` or `send`. `reason` when not ok: `invalid`, `unknown_task`, `not_here` (the task belongs to another conversation: answer it there), or the core's (`duplicate`, `locked`, ...). |
| `toast`, `error` | `text` | Something to show briefly. |

Messages reach each client in the order the Classroom made them (one queue per connection). A client that stops reading
falls behind and is disconnected; it reconnects and gets a new `hello`.

### Events to the Classroom

| Type | Fields | Does |
| --- | --- | --- |
| `start` | `id`, `project`, `text`, `workflow` (a name from `workflows`), `crew` (seat names) | A new task. |
| `send` | `id`, `task`, `text`, `action` (a button's label, optional) | Your message to a task: more work, an answer, or a `$command` such as `$approve <id>` (with `action`, the page shows it as a choice). |
| `accept` | `id`, `task`, `text` (the brief), `project`, `workflow`, `crew` | Take up an invitation (status `invited`; it is `queued` at once, so a second accept is refused). |
| `load` | `task` | A task's steps and messages, answered with `loaded` in order with the events: an event sent before the snapshot is in it, one sent after comes after it. |
| `tick` | `task`, `item`, `checked`, `fingerprint` | Tick (`checked: true`) or untick a second-check item: the same ticks as `$check`. With `fingerprint`, only while that is still the current change set. |
| `folder` | `name` | Where a new project of that name would go. |
| `report` | `task` | Send the report to the conversation the task came from (`$call core.send_message`, as you); `reported` is set once the core says it was delivered. |
| `add_project`, `create_project`, `remove_project` | `name`, `path`, `test` | Projects. |
| `forget` | `task` | Remove a finished task from the list. |

Everything else a person can do is a `$command` sent with `send` (and from any other interface, Discord included):
`$check [task] [all | 1 2 3]`, `$approve <id>`, `$deny <id>`, `$land [task]`, `$discard <task>`, `$tasks [words]`,
`$projects`.

## Places (contracts 1.7)

The Classroom publishes itself in the core's directory as a place: `{"place": {"name": "Classroom", "icon": "📝",
"note"}, "people": [{"key", "name", "avatar", "doing", "title", "task"}], "away": [keys], "tickets": [{"key", "by",
"title", "link", "task", "avatar"}], "working", "ready", "views": [{"name", "icon", "path", "order"}]}`. `views` are
the page's places for the shared places bar (Classroom, Students `#students`, Logbook `#logbook`). A key is the name
lowercased with only its letters and digits kept, in any script ("Flash-chan" → "flashchan", "露娜" → "露娜"); an
empty key matches nobody. `people` are the students on a visit (the one who brought you, her crew, whoever worked on
it); `away` are the names of people from elsewhere who are here now (MomoChat's DeepSeek, while Flash-chan stands for
her); `tickets` are invitations held by people elsewhere. Any
interface reads the places with `port.directory()`; this API passes them on as `places`.
