# `webui`: the bot as a chat page on your local network

An external module (in `plugins/`, tracked in git, loaded like any other module) that serves a chat page
from the bot itself. Open `http://<this computer>:4156` from a laptop or phone at home and talk to the
bot without Discord: each chat is a conversation of the shared room (Luna, Gemini, Claude, DeepSeek and
Grok) by default, with Markdown and LaTeX, files both ways, live "typing" lines, approval cards with
buttons, and a per-chat model menu. Whoever opens the page acts as the bot's creator.

The user-level tour is in [plugins/README.md, "The web UI"](../README.md#the-web-ui). This README is
the developer companion: it is the project's example of an interface plugin, so read it when you want
to connect the core to a new platform, especially one you host yourself.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `webui` (renamable with the `interface` key) | `Display(markdown=True, edits=False, progress=True, quotes=False, files=True, max_upload_mb=50, max_text_chars=20000)`; the last two come from `max_upload_mb` and `max_text_chars`. No `operations`. |
| Default assignment | `webui:*` | `{engine: room, personas: [luna, gemini, claude, deepseek, grok], budget: {model_calls: 40, tool_calls: 24, seconds: 900}, options: {history_limit: 40, compact: true, chime_in: true, diary_scope: webui}}`. `personas` and `assignment` in the config change it; core.yml wins over both. |
| Runtime assignments | `webui:<chat>` | One per chat started with some of the classmates (not all), set with `core.set_assignment` and removed with the chat (below). |
| Default admission | `webui:*` | `{enabled: true, require_mention_in_groups: false}` |
| Owner report | `port.set_owners(["creator"], control=webui:classroom, fallback=True)` | Used only when core.yml lists no `webui` owners; the classroom becomes the control route only when no other interface offers one ([ARCHITECTURE.md, Owners and control](../../docs/core/ARCHITECTURE.md#owners-and-control)). |

It contributes no engines, capabilities, personas, profiles or permissions. The five personas and the
`room` engine come from [`extensions/room`](../../extensions/room).

Declared settings (editable with `python -m core setup` and `$setup`):

| Key | Kind | Default | Env var |
| --- | --- | --- | --- |
| `port` | number | 4156 | |
| `user_name` | text | your login name | |
| `title` | text | `Study Room` | |
| `passphrase` | secret | empty (no sign-in) | `WEBUI_PASSPHRASE` |

## Setup

It loads by default. To change anything, copy [`config.example.yml`](config.example.yml) to
`config.yml` in this folder (git-ignored); `extension_config.webui` in core.yml overrides it.

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | 4156 | TCP port. `0` picks a free one (the tests do this). |
| `host` | `0.0.0.0` | Address to listen on. `127.0.0.1` keeps it to this computer. |
| `user_name` | login name | Your name on your own bubbles. |
| `title` | `Study Room` | Page title and the name shown for messages without a speaker. |
| `passphrase` | empty | Asked once per browser. Prefer `WEBUI_PASSPHRASE` in `.env`; if you put it in `config.yml`, `chmod 600 config.yml`. |
| `networks` | private, loopback and link-local addresses | A list of CIDR networks that may connect *instead of* the default (for example to add Tailscale's `100.64.0.0/10`, list your LAN too). |
| `hostnames` | none | Extra names this computer is reached by. IP addresses, `localhost` and names ending in `.local`, `.lan`, `.home.arpa` or `.internal` always pass. |
| `personas` | the five classmates | Personas of the default `webui:*` assignment, and who the new-chat picker offers. |
| `classmates` | `[Luna, Gemini, Claude, DeepSeek, Grok]` | Display names of those personas, in the same order (a missing one is the persona's name, capitalised). They should match the personas' display names: the page finds a classmate's model row by exact display name and a speaker by prefix. |
| `assignment` | | Merged over the default assignment (top-level keys replace, so `budget` replaces the whole budget; `options` are merged key by key over the web UI's). |
| `max_upload_mb` | 50 | Largest upload and largest file the core sends here; also sizes the server's request limit. |

Keys the code reads that the example file does not list: `interface` (the interface name, default
`webui`), `passphrase_env` (the env var to read the passphrase from, default `WEBUI_PASSPHRASE`),
`user_id` (your sender ID, default `creator`) and `max_text_chars` (20000).

Environment: `WEBUI_PASSPHRASE` (optional). Python package: `aiohttp` (in `requirements.txt`; `discord.py`
needs it too). The browser loads KaTeX 0.16.11 from jsDelivr, pinned
with integrity hashes; without internet access math is shown as raw TeX.

Pictures for the classmates go in [`avatars/`](avatars/README.md). The page's icon art is credited in
[`static/CREDITS.md`](static/CREDITS.md) (CC BY-NC-SA 4.0).

To turn it off, in core.yml:

```yaml
modules:
  exclude: [webui]          # or plugins/webui
```

At startup it prints the address to open, for example `[webui] Open http://192.168.1.20:4156 on this
network (passphrase required).`

## Using it

See [plugins/README.md, "The web UI"](../README.md#the-web-ui) for what a person sees: the chat list,
the class list with presence and the model menu, `@` mentions, the `$`/`/` command palette, work notes,
files and approval cards. Messages you send are always direct and addressed to the bot
(`is_direct=True`, `mentions_me=True`), so every chat behaves like a private conversation with the room.

**Who is in a chat.** `＋ New chat` opens a picker with the classmates (all ticked). Untick some to start
a smaller chat; leave one for a direct message with her. The 💬 button on a classmate's card in the class
list also opens your direct message with her (the existing one if there is one). Direct messages are
listed under their own heading in the sidebar, with her picture, and the class list shows only the
chat's members.

**How the room behaves here.** The web UI's assignment sets the room's `options`: each classmate reads
the last 40 messages word for word, older ones are folded into the chat's rolling summary
(`compact`), a message naming no one gets an answer from whoever's turn it is while the others may chime
in or stay silent (`chime_in`), and every web chat shares one diary scope (`diary_scope: webui`), so
what Luna learns about you in the classroom she still knows in your direct messages with her. Each note
stays filed in the chat it was written in; the scope only lets the others read it, so changing or removing
it loses nothing. Change any of these under
`assignment: {options: ...}` (see [the room's options](../../extensions/room/README.md#setup)).
With `chime_in`, a message to the whole class can take up to five model calls.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `EXTENSION` with its settings; `setup` reads the passphrase (config or env), builds the interface and contributes the default assignment and admission. |
| [`interface.py`](interface.py) | `WebUI(Interface)`: an aiohttp server (pages, files, uploads, history, one websocket per open page), the security guard, page events to `Request`s, and `deliver` for every `Reply` kind. |
| [`store.py`](store.py) | `Store`: the page's transcript in SQLite (`webui.db`): chats, messages as the JSON the page renders, and file records. |
| [`static/index.html`](static/index.html), [`static/login.html`](static/login.html) | The chat page and the sign-in page (`{{title}}` and `{{error}}` are filled in HTML-escaped). |
| [`static/app.js`](static/app.js) | The page: websocket client, rendering, chat list, class list and model menu, composer, palette, uploads. |
| [`static/markdown.js`](static/markdown.js) | A small Markdown renderer that escapes the text first and adds only its own tags (links only to http/https); math is typeset by KaTeX when loaded; `@Name` becomes a chip. |
| [`static/style.css`](static/style.css) | Layout and theme (light and dark). |
| [`avatars/`](avatars/README.md) | Classmates' pictures, served at `/avatars/<key>`. |

### HTTP routes

| Route | Does |
| --- | --- |
| `GET /` | The chat page (`no-cache`). |
| `GET /login`, `POST /login` | Sign-in page and form, only meaningful with a passphrase. |
| `GET /static/{name}` | Only names matching `[a-z0-9_-]+\.(js|css)` in `static/`. |
| `GET /avatars/{key}` | A picture in `avatars/` whose stem is `key` (png, jpg, jpeg, webp, gif). |
| `GET /favicon.ico` | `static/favicon.png`. |
| `GET /files/{file_id}` | A stored upload or delivered file (below). |
| `GET /api/history?conversation=&after=` | A chat's stored messages: the latest 300, or those after a message ID (not a whole number from 0 to 2^63-1: 400). |
| `POST /api/upload` | Multipart upload; each part is written to `uploads/<id>/<name>` and recorded; returns `{files: [{id, name, mime, size}]}`. More than `max_upload_mb` in all: 413, and nothing of it is kept. |
| `GET /ws` | The websocket (heartbeat 30 s, messages up to 1 MiB). |

### From page events to requests

The page never answers anything itself; everything a person types goes through `port.submit`, as
[EXTENSIONS.md, Submitting requests](../../docs/core/EXTENSIONS.md#submitting-requests) requires.
`_send` (interface.py:547) builds the `Request`:

* `id` is `web-<id>`, where the page makes `id` (a random UUID per message). If the page resends after
  a reconnect, the core drops the duplicate and the page still gets a positive `ack`.
* `address` is `webui:<chat>`; chat IDs must match `^[a-z0-9][a-z0-9-]{0,47}$` (the default chat is
  `classroom`, new ones are `chat-<base36 time>`, direct messages `dm-<base36 time>`).
* A message to a chat whose own assignment is still being set (below) waits for it first (at most 25 s),
  so it is never answered by the whole class. If the setup fails (the chat is deleted first) or is still
  unfinished, the message is not submitted: `ack` with `ok: false`, reason `chat_not_ready`. A message to
  a chat that does not exist (deleted, perhaps on another device, or removed after a failed setup) is
  refused the same way with `unknown_chat`: sending never creates a chat, which would bring it back with
  the whole class. A chat with some of the classmates whose assignment the core no longer has (a crash
  cut its setup off) gets it again first (`_repair`); if that fails, `chat_not_ready`. The chat is read
  again after that wait: deleted meanwhile (on another page), the message is refused with `unknown_chat`
  and the assignment the repair set is removed.
* `sender` is `Sender(user_id, user_name)`; `text` is cut to `max_text_chars`; up to 10 previously
  uploaded files become `FileRef`s; `kind` is `text`, else `image` if all files are pictures, else `file`.
* Only after `submit` is accepted is the message stored and broadcast, so the transcript holds what the
  core actually took. The first line of the first non-command message names a "New chat" (48 chars).
* A pressed button sends its `Action.command` as ordinary text with `action` set to the label; the
  transcript shows it as `You chose “Approve”`. Typed or pressed `$approve <id> [always]` / `$deny <id>`
  also marks the matching approval card as decided (`_mark_decision`, `Store.decide`), which removes its
  buttons on every open page.
* `/name` in the composer becomes `$name` when `name` is a known command; the palette lists
  `port.commands()` (sent in the `hello` state with their usage and examples).
* Deleting a chat calls `port.cancel(webui:<chat>, ...)`, then removes its transcript and files from
  the page, and its own runtime assignment if it has one. The core keeps its own history.

### Quiet commands: the model menu and each chat's classmates

The page uses the core's ordinary `$commands` instead of a private API, but keeps them out of the chat:

* `_quiet_command` (interface.py:410) submits a command with ID `web-quiet-<uuid>`, remembers that ID in
  `self._quiet` with its purpose and a future (at most 100 model-menu asks; the oldest go first, their
  futures answered `lost`; a chat's setup is never dropped, since its answer is needed for clean-up), and
  returns the future: `{ok, code, text, result}`, or `ok: false` with the reason if
  `submit` refused it. Every value that goes into a command is checked first (`persona` and `model`
  against strict patterns, personas against the roster, JSON-encoded arguments), so nothing else can be
  smuggled into the command text.
* In `deliver`, before anything else, a reply whose `in_reply_to` is a quiet ID goes to
  `_answer_quietly` (interface.py:608): its progress and `waiting_approval` notices are swallowed, and
  the answer resolves the future (with `reply.data["result"]`, the command's result data, as JSON)
  instead of being stored as a bubble. A reply to a quiet ID no longer awaited (dropped, or from before a
  restart) is swallowed the same way: never a bubble, and never a reason to re-create a deleted chat.
* **The model menu**: `$model` (purpose `overview`) and `$model <persona> <provider>/<model|reset>`
  (purpose `choose`). Their answers are broadcast as `models` events; a refusal goes to the asking page.
* The page shows the overview's `personas` (`name`, `display_name`, `model`, `chosen`), `models` (per
  provider) and `errors`; after a choice it shows the answer as a toast and asks again. If the core
  answers `not_permitted` (`$model` is for admins), the page hides the menu.

* **A chat's classmates** (`_new`, `_assign`, interface.py:450-479): for a `new` event with `personas`,
  the chat is stored with them and shown at once. If they differ from the personas of the assignment the
  chat has now (`port.policy(address)`, normally `webui:*`), the interface sends
  `$call core.set_assignment {"pattern": "webui:<chat>", "assignment": {...}}`: that assignment with only
  `personas` changed (purpose `assign`). As the creator's own `$call`, it needs no approval. Whether it
  worked is read back from `port.policy`, not from the answer's wording; on failure (or no answer within
  20 s) the chat is deleted again and the page gets an `error` event.
* **Removing them** (`_unassign`, `_tidy`): before asking, the chat is recorded in `webui.db`
  (`assigned`), and only recorded chats are ever unassigned, so an assignment configured in core.yml or
  set by someone else for a `webui:` address is never touched. Deleting a chat sends
  `$call core.delete_assignment` (purpose `unassign`) and drops the record once the policy shows it gone.
  A setup answered after its chat was given up (the core was slow) is removed as soon as that late
  answer arrives. A chat deleted while its setup runs is unassigned once the setup answers. 5 s after each
  start, `_tidy` makes the core match the page: it removes the assignments of recorded chats that no
  longer exist (a deletion cut off by a restart), and sets again those a chat with some of the classmates
  lacks (a setup cut off by a restart or a crash). A picker chat's own assignment from before the
  `assigned` record existed is adopted into it (`_adopt`, also when deleting), so it is removed too. On stop, setups still waiting are given up at once and
  their chats kept for `_tidy`, so stopping never waits for the core.

The core still records these commands like typed ones; the room leaves commands out of what its
characters read. A runtime assignment copies the `webui:*` one when the chat is made, so later changes
to `webui:*` (in core.yml or the config) do not reach chats started with some of the classmates.

### Rendering each reply kind

`deliver` (interface.py:628) maps the reply to a stored item and broadcasts it:

| `reply.kind` | Server | Page |
| --- | --- | --- |
| `progress` | Not stored. Kept in memory per `run_id` (or chat) and broadcast as `progress`; sent again in every `hello`. | A typing line with the speaker's avatar ("Luna is using …"), its `Cancel` button, and "typing…" on that classmate in the class list. Lines older than 15 minutes are dropped. |
| `text` | Stored with `speaker`, `label`, `data` (including `provider` and `model`), `actions`, `origin`. | A bubble with avatar, name, time and the model that wrote it (with ⚠ when a fallback answered; hover for the reason); a `label` (work note) folds into a `<details>`. Markdown and math rendered. |
| `file` | The file is copied out of the run workspace into `files/<id>/<name>` (workspaces are deleted later) and recorded. | Pictures inline (click to enlarge), audio and video as players, anything else as a download link. |
| `notice` `approval_request` | Stored; indexed by `approval_id`. | A card: tier badge, summary, warnings, who asked and where, the exact arguments folded, and the action buttons, or "✓ approved" / "✕ denied" once decided. |
| `notice` `reminder` | Stored. | A reminder card. |
| other `notice` | Stored. | A pill: `reply.text`, else a phrase for the code (`run_failed`, `waiting_approval`, `unknown_command`, `not_permitted`, `locked`; `relayed` from its data). |
| `event` | Stored. | A pill "📨 text", with the origin address. |

Any non-progress reply with a `run_id` first clears that run's typing line (`progress_clear`). Replies
for a chat the page does not know create it. `deliver` returns `Delivery(True, message_id=<stored
row ID>)`, `Delivery(False, detail="missing_file")` for a missing file, or `not_started` before
`start`.

### The websocket protocol

One socket per open page; every event is a JSON object with `type`. The server sends `hello` first. An
event that fails (a field of the wrong type, say) is logged as `event_failed` and skipped; it never closes
the connection.

Page to server:

| `type` | Fields | Effect |
| --- | --- | --- |
| `send` | `conversation`, `id`, `text`, `files` (upload IDs), `action` | Submit a request (above); answered with `ack`. |
| `new` | `conversation`, `title`, `personas` (optional: persona names from the roster) | Create a chat, with those classmates only (above). Ignored for an existing chat. |
| `rename` | `conversation`, `title` | Rename (80 chars). |
| `pin` | `conversation`, `pinned` | Pin or unpin. |
| `order` | `conversations` (IDs) | Arrange the list (pinned stay first). |
| `delete` | `conversation` | Cancel its runs, delete transcript and files. |
| `models` | `conversation` | Quiet `$model` overview. |
| `model` | `conversation`, `persona`, `model` (`provider/model` or `reset`) | Quiet `$model` choice. |

Server to page (`_broadcast` sends to every open page, so several devices stay in step):

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `state`: `title`, `user`, `classmates`, `roster` (`[{name, display}]`), `avatars`, `commands`, `conversations` (each with `personas`: a list, or null for the whole class), `progress`, `default`, `max_upload_mb` | Sent on connect. |
| `message` | `item` | A new stored message (yours or the bot's). |
| `update` | `item` | A stored message changed (an approval card decided). |
| `conversations` | `items` | The chat list changed. |
| `deleted` | `conversation`, `items` | A chat was deleted. |
| `progress` | `conversation`, `run_id`, `text`, `actions`, `at` | A run's typing line. |
| `progress_clear` | `conversation`, `run_id` | Remove that line. |
| `models` | `conversation`, `purpose`, `ok`, `code`, `text`, `result` | Answer to a quiet `$model`. |
| `ack` | `id`, `ok`, `reason` | Only to the sender: the `SubmitResult` reason (`accepted`, `command`, `duplicate`, `disabled`, ...). |
| `error` | `text` | Only to the sender: a chat could not be started (unknown classmates, or the core refused its assignment). |

The page reconnects with backoff (0.5 s doubling to 10 s), queues events while disconnected, and
fetches `/api/history?after=<last id>` after each `hello`, so nothing is missed.

### Security

Whoever opens the page has creator rights, so the server treats every request as hostile until proven
local. The `_guard` middleware (interface.py:194) runs on every route, the websocket included:

1. **Peer address**: only loopback, private and link-local addresses (or exactly `networks:`); IPv4
   mapped IPv6 is unwrapped. Otherwise 403.
2. **Host header**: an IP address, `localhost`, a local suffix or a configured hostname. A public name
   that resolves here is a DNS-rebinding attempt by some website. Otherwise 403.
3. **Origin**: if present, it must equal `<scheme>://<Host>`, which stops other sites in the same
   browser from posting or opening the websocket. Otherwise 403.
4. **Passphrase** (if set): everything except `/login`, `/static/style.css` and `/favicon.ico` needs the
   `webui_session` cookie; `GET /` redirects to `/login`, the rest get 401. The cookie value is an
   HMAC-SHA256 of the passphrase under a random key in `session.key` (mode 600), compared in constant
   time; it is HttpOnly, SameSite=Strict, 30 days. Changing the passphrase signs every browser out.
   Wrong tries wait one second, and five within five minutes from one address lock it out for a while.
5. **Headers** on every HTTP response: a Content-Security-Policy (`default-src 'self'`, scripts only
   from the page and the pinned KaTeX file, `connect-src 'self'`, `frame-ancestors 'none'`,
   `object-src 'none'`, ...), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
   `X-Frame-Options: DENY`.

Content safety: files are served inline only for pictures, audio, video and PDF; anything else (an
HTML or SVG file could run script) is sent as `application/octet-stream` with `attachment`. Stored
file names are sanitised; static and avatar names must match strict patterns, so no path reaches outside
those folders. Model text is never inserted as HTML: `markdown.js` escapes it first. IDs from the page
(chat, persona, model) are validated before use.

## Data it keeps

Under its data directory (`<data_dir>/extensions/webui/`, by default `data/core/extensions/webui/`):

| Path | Contents | Kept |
| --- | --- | --- |
| `webui.db` | Chats (title, position, pinned, personas), each delivered or sent message as the JSON the page renders, file records, and the chats it asked the core for a runtime assignment for (`assigned`). | Until the chat is deleted on the page (`assigned`: until its assignment is gone). |
| `uploads/<id>/<name>` | Files you uploaded. | Until the last chat that showed them is deleted. Uploads never sent are not removed. |
| `files/<id>/<name>` | Copies of files the bot sent. | Until their chat is deleted. |
| `session.key` | The random key for the sign-in cookie. | Always; delete it to sign every browser out. |

Progress lines and quiet requests live only in memory. The core keeps the real conversation history
separately, as for any interface, and each smaller chat's runtime assignment until the chat is deleted.
The classmates' diaries about you are the core's, filed per chat and read across the `webui` scope. The browser keeps the current chat, panel widths and open panels in
`localStorage`.

## Tests

[`tests/test_plugin_webui.py`](../../tests/test_plugin_webui.py) starts a real core with this plugin on
`127.0.0.1`, port 0, and a probe engine, then drives it over HTTP and the websocket. It checks: which
peers and Host headers pass; that a message reaches the engine as the creator and the answer arrives
live and stays in history; the quiet model menu (overview, choose, reset, no bubbles, a malformed model
never sent); new chats as their own conversations titled by their first line; chats started with some
classmates (with the room and fake models: only they answer, a message sent at once waits for the
assignment, everything else is copied from `webui:*`, nothing shows in the chat), one classmate as a
direct chat, the whole class or unknown names setting no assignment, deleting a chat removing its
assignment, and a chat the core cannot staff not being kept; a message queued behind a failed setup not
reaching the whole class (from the same page or another); an assignment that lands after its chat was
given up being removed; and the start-up sweep removing only this page's own leftovers; pinning, ordering and
deleting with files; approval cards and button presses; typing lines until a run speaks; files both
ways and inline only for media; avatars and nothing else from the folders; refusal of other sites and
rebound names, and the CSP; the passphrase flow and cookie flags; and the default room assignment.

```bash
venv/bin/python -m unittest tests.test_plugin_webui
```

## Building something similar

* **Contribute defaults for your own interface only** (extension.py:37-41): an assignment and admission
  for `webui:*` make the plugin work on drop-in; core.yml still wins.
* **Report the owner and a fallback control route** in `start` (interface.py:146) when your platform
  has a natural owner, as the CLI does at a terminal.
* **Return promptly from `start`**: aiohttp's `AppRunner`/`TCPSite` run in the background; on a bind
  failure call `port.set_ready(False, ...)` and raise (interface.py:157-164).
* **Submit first, store after**: record the person's message only when `submit` accepted it, and use a
  page-made ID so resends are deduplicated (interface.py:547-583).
* **Copy delivered files out of the run workspace** before referring to them later (interface.py:307).
* **Key progress by `run_id` and clear it on the run's next reply** (interface.py:641-658).
* **Talk to the core with commands, not private hooks**: the quiet command pattern (interface.py:410
  and 608) shows how a page feature can use any `$command` (here `$model` and `$call
  core.set_assignment`) and read `reply.data["result"]`; check the effect through `port.policy` where
  you can rather than parsing the answer.
* **Keep `deliver` safe to call concurrently**: the store serialises with a lock, and `_broadcast`
  gives each socket 5 seconds and drops it on failure, so one stuck browser cannot hold up delivery.
* **Bound every in-memory map** that a client can grow (`_quiet`, login failures).
* **Pitfalls**: a page that acts as the creator is as powerful as a remote shell, so do not drop the
  Host/Origin checks even on a home network; never inject model or file text as HTML; never serve
  arbitrary file types inline; validate every ID the client sends before putting it in a path, a query
  or a `$command`.

See also: [docs/core/EXTENSIONS.md, Interfaces](../../docs/core/EXTENSIONS.md#interfaces),
[Delivering replies](../../docs/core/EXTENSIONS.md#delivering-replies),
[docs/core/ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md),
[plugins/README.md](../README.md), [`extensions/cli`](../../extensions/cli/README.md) (the smallest
interface), [`extensions/room`](../../extensions/room) (the engine behind the classmates),
[`extensions/discord`](../../extensions/discord/interface.py) (a full platform).
