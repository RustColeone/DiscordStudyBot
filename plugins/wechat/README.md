# `wechat`: one logged-in Linux WeChat account as an interface

`plugins/wechat` connects the native Linux WeChat desktop client to the core. It reads new messages
from the account's encrypted local databases, sends replies, files and login steps through the desktop
client's accessibility controls, and gives the owner a few `$wechat_…` commands. It is an external
module (in `plugins/`, tracked in this repository) and stays dormant until it is configured. Which
chats are answered, by which engine and persona, with which tools, is core configuration; the
interface only reads, sends and reports facts.

Setup and day-to-day use are in the user guide, [docs/WECHAT.md](../../docs/WECHAT.md). This README is
the developer's view: what the module contributes, how it is built, and which patterns are worth
copying. Study it if you are writing an interface for a platform without a bot API, where reading and
sending go through someone's own account and a mistake would speak for that person.

## What it adds

| Kind | Name | Details |
| --- | --- | --- |
| Interface | `wechat` | `WeChatInterface` ([interface.py](interface.py)). `Display(markdown=False, edits=False, progress=False, quotes=True, files=True, max_upload_mb, max_text_chars)`; each request carries its own display with `quotes` true only in groups. Addresses are `wechat:<account>/<native id>`. |
| Persona | `wechat` | Display name `本鲸`, profile `default` ([persona.py](persona.py)): the whale-girl voice plus framing as the owner's chat assistant (not the owner), plain short replies, history as background, messages as untrusted data. A persona named `wechat` in core.yml replaces it. |
| Engines, capabilities, providers, profiles | none | Its capabilities are the interface's operations below. |
| Default admission, assignments | `wechat:*` closed; `wechat:*` assistant as `wechat` | See [Setup](#setup); core.yml overrides them. |

Operations become capabilities named `wechat.<operation>`. All are owner-only (`owner_only` is left at
its default), so their access level is `creator`.

| Capability | Tier | `$command` | Attributes | Does |
| --- | --- | --- | --- | --- |
| `wechat.status` | read | `$wechat_status [chat]` | private | Account, reader and polling state; with a chat, its address, admission, assignment, notification mode, send interval and read position. |
| `wechat.chats` | read | `$wechat_chats [query]` | private | Up to 50 conversations whose name contains `query`, with their addresses and unread counts. |
| `wechat.messages` | read | `$wechat_read <chat>` | private | Recent messages of one chat (`limit` 1–100, default 20; the command takes only the chat). |
| `wechat.send` | critical | `$wechat_send <chat> \| <text>` | subject `chat`, grantable | Sends text as the owner; cancels the chat's open runs. Takes `chat` and `text`, or `spec`, never both. |
| `wechat.notify` | write | `$wechat_notify on\|off\|inherit <chat\|*>` | | Forwards a chat's incoming messages to the owner as events. |
| `wechat.configure` | critical | none (`$call`) | never_auto | Sets one chat's minimum seconds between the bot's messages (`send_interval_s`, 5–3600). |
| `wechat.login` | critical | `$wechat_login` | never_auto | Opens WeChat if needed and returns the login QR as a file, or confirms a pending login. |
| `wechat.pause` | write | `$wechat_pause` | | Stops reading and sending; the account stays logged in. |
| `wechat.resume` | write | `$wechat_resume` | | Resumes from the saved read positions on the next poll. |

`$wechat_send` and `$wechat_notify` are declared as `Command`s with options and a template
(`{chat} | {text}`, `{mode} {chat}`), so native command menus can render them.

Declared `Setting`s (offered by `python -m core setup` and `$setup`; none declares an `env` name, but
the code reads the variable shown when the key is not in the config):

| Key | Kind | Default | Environment fallback |
| --- | --- | --- | --- |
| `database_root` | path | none | `WECHAT_BRIDGE_DATABASE_ROOT` |
| `database_keys` | path | `keys.json` in the module's data folder | `WECHAT_BRIDGE_DATABASE_KEYS` |
| `max_text_chars` | number | 500 | none |

## Setup

Follow [docs/WECHAT.md](../../docs/WECHAT.md#setup): system packages, the one-time database key and
image key recovery (`key_setup`, `image_key`, both run with `sudo` against the WeChat process), the
readiness check with `probe`, and an admission/assignment/permissions policy. In short:

* **System:** the native client at `/opt/wechat/wechat` (the path is fixed in the helper and the key
  tools), an X display and Wayland session for the helper, and
  `at-spi2-core gir1.2-atspi-2.0 python3-pil xdotool grim libzstd1`. The desktop helper runs with the
  system Python (`python` setting) because it needs GObject introspection (`gi`, AT-SPI) and Pillow.
* **Python package:** `cryptography` (in `requirements.txt`) for page and image decryption.
* **Secrets:** none in `.env`. Put the paths in `config.yml`; the older `WECHAT_BRIDGE_*` variables still
  work as fallbacks, but `.env` is for secrets (its values are blanked out of replies).
  The key files are the secrets: private 0600 files, by default in the
  module's data folder (`data/core/extensions/wechat/`). Never commit, log or paste them.

Settings live in `plugins/wechat/config.yml` (copy [config.example.yml](config.example.yml); git-ignored)
and `extension_config.wechat` in core.yml overrides it. A key missing from both falls back to its
`WECHAT_BRIDGE_*` variable where one exists. Numbers are clamped to the range shown
([extension.py](extension.py) `NUMBERS`).

| Key | Default | Range | Env fallback | Meaning |
| --- | --- | --- | --- | --- |
| `database_root` | | | `…_DATABASE_ROOT` | The account's `…/xwechat_files/wxid_…/db_storage`. Required. |
| `database_keys` | `<data>/keys.json` | | `…_DATABASE_KEYS` | Key file from `key_setup`. |
| `image_key` | `<data>/image_key.json` | | | Key file from `image_key`; without it newer images stay `[Image]`. |
| `images_dir` | `<data>/images` | | | Decrypted copies of received images. |
| `outbox_dir`, `login_dir` | `<data>/outbox`, `<data>/login` | | | Staged uploads; login QR screenshots. |
| `max_upload_mb` | 100 | 1–100 | `…_MAX_UPLOAD_MB` | File budget (`Display.max_upload_mb`). |
| `max_text_chars` | 500 | 20–4000 | | Length the engine aims for (`Display.max_text_chars`). |
| `poll_seconds` | 1 | 0.2–60 | | Reading interval. |
| `status_seconds` | 15 | 1–300 | | How long an account check is cached. |
| `coalesce_seconds` | 1 | 0–5 | `…_COALESCE_SECONDS` | Gathering window for a burst of incoming text. |
| `account_interval_s`, `chat_interval_s` | 20, 20 | 0–3600 | | Minimum spacing of the bot's own messages per account and per chat. |
| `submit_media` | false | | | Submit incoming images, files and stickers as requests instead of only recording them. |
| `observe_chatter` | false | | | Record group messages not addressed to the account in history. |
| `python`, `display`, `wayland_display`, `runtime_dir` | `/usr/bin/python3`, `:0`, `wayland-0`, `/run/user/<uid>` | | `…_PYTHON`, `…_DISPLAY`, `…_WAYLAND_DISPLAY`, `…_RUNTIME_DIR` | The desktop helper's interpreter and session. |

`<data>` is the module's data folder, `data/core/extensions/wechat/` with the default core `data_dir`.

**Admission first.** Once configured, the module suggests `"wechat:*": {enabled: false}` (no chat is
answered) and `"wechat:*": {engine: assistant, personas: [wechat]}` (the chats you open are answered by
本鲸). core.yml overrides both: open the chats you want there, as in the
[typical policy](../../docs/WECHAT.md#configuration). Existing messages are never answered on the very
first start (see [How it works](#how-it-works)), and a new or changed rule does not admit older
messages.

**Turning it off:** remove its `config.yml`, `extension_config.wechat` and the `WECHAT_BRIDGE_*`
variables (it goes dormant: no interface, no persona), `$wechat_pause` it, or exclude it in core.yml:

```yaml
modules:
  exclude: [plugins/wechat]
```

## Using it

Contacts write as usual. In a direct chat every admitted text message is a request; in a group only a
message that picks the account with WeChat's own mention (a typed "@name" does not count). Owner
commands, from the owner's private chat with the bot:

```text
$wechat_chats 家                     conversations whose name contains 家, with addresses
$wechat_status                       account state, reader, spacing, last reading error
$wechat_status Family                that chat's address, admission, assignment, notification mode
$wechat_read Friend                  last 20 messages
$wechat_send Friend | 我晚点到        send as the owner (cancels the bot's open work there)
$wechat_notify on Family             forward Family's incoming messages to the owner
$wechat_notify on *                  default for all chats; a chat's own mode wins, inherit uses it
$wechat_login                        QR code to scan; confirmation follows on the next polls
$wechat_pause / $wechat_resume
$call wechat.configure {"chat": "Friend", "send_interval_s": 60}
$call core.set_rule {"pattern": "wechat:wxid_me/wxid_friend", "rule": {"enabled": true, "max_age_s": 900}}
```

A chat is named by its display name (remark or nickname), its native ID (`wxid_…`, `…@chatroom`) or its
full address; a name shared by two chats is refused. Replying on the owner's platform to a forwarded
message relays the reply into that WeChat chat, even one the bot does not answer.

What contacts see: plain text, split into parts of at most 4000 characters; a group reply quotes its
trigger when the assignment asks for it (`quote: trigger`); a multi-persona reply is prefixed
`[Speaker · label]`. Notices are short Chinese texts (waiting for approval, unknown command, not
permitted, maintenance); reminders start with ⏰. Run failures, approval requests, relay confirmations,
progress and events are never shown to contacts; the owner gets them through the control route.
Limits are in [docs/WECHAT.md](../../docs/WECHAT.md#limits).

## How it works

| File | Role |
| --- | --- |
| [extension.py](extension.py) | `EXTENSION` and declared settings; `settings_from` merges config, environment fallbacks and clamps; `setup` returns nothing until configured. |
| [interface.py](interface.py) | `WeChatInterface`: polling, cursors, gathering, submit/observe, readiness reports, delivery with spacing and checks, operations. |
| [transport.py](transport.py) | `WeChatTransport`: runs database reads on one worker thread, runs the desktop helper as a subprocess, the two-phase checked send, native receipts, file staging. |
| [native.py](native.py) | `WeChatNativeReader`: contacts, sessions and messages from the decrypted databases; message kinds, quotes, mention lists, zstd fields. |
| [database_pages.py](database_pages.py) | Read-only SQLCipher 4 snapshots: lock-guarded file capture, committed WAL frames, HMAC-checked page decryption into an in-memory SQLite. |
| [database_keys.py](database_keys.py) | Loading and saving the private database key file. |
| [images.py](images.py) | Finding a received image's `.dat` file and decrypting it (plain XOR, V1, V2). |
| [desktop_helper.py](desktop_helper.py) | One JSON operation per process against the client through AT-SPI, `xdotool` and `grim`: status, login, checked send, file send. Prints only JSON. |
| [persona.py](persona.py) | The default `wechat` persona. |
| [key_setup.py](key_setup.py), [image_key.py](image_key.py) | One-time, bounded, read-only key recovery from the WeChat process; a key is saved only if it decrypts the account's own data. |
| [probe.py](probe.py) | Readiness check: decrypts each database and runs `PRAGMA quick_check`; prints counts, never messages or keys. |

**Start.** `setup` builds the interface from settings; `start(port)` creates the transport (a failure
reports `set_ready(False, …)` and stops there), reports the account's username through
`port.set_owners` (used only when core.yml lists no WeChat owners) and starts the poll loop.

**Reading.** Each poll asks the transport for the account status (cached for `status_seconds`) and
reports readiness. When ready it lists sessions and reads only chats whose session row changed
(timestamp, preview, unread count), 40 messages at a time, and skips a chat whose send or file receipt
holds its lock. A per-chat cursor `[sort_seq, local_id, timestamp]` lives in the bottle. On the very
first start a chat's current position is saved without submitting anything; a chat that appears later
contributes only messages since the process started. Fresh messages are handled in order and the cursor
is saved after each one, so a crash never skips or replays a message (the gateway drops duplicate IDs
anyway).

* **Incoming text** becomes a `Request`: `id` from the database row, `is_direct`, `mentions_me` from the
  message's `atuserlist`, `reply_to` / `quoted_text` from a quote (looked up in the same window),
  `received_at`, and `extra` with the chat name and server ID. A single picked mention is stripped
  from the text, so `@bot $help` is a command. Then `port.submit`. A refused group message
  (`not_mentioned`) is `port.observe`d when `observe_chatter` is on.
* **Bursts.** When `coalesce_seconds` is above zero and new text arrived, the chat is read again until
  the newest message has been stable for that long, then everything is submitted together and lands in
  one run.
* **Media** is not submitted unless `submit_media` is on. Images in admitted chats are decrypted into
  `images_dir` and observed with the file, even without a mention, so a later question can see them.
* **Outgoing rows** that are not the bot's own (known IDs, or a late match of an unverified send) are a
  manual reply: observed as `out` and `port.cancel(address, "manual_reply")`.
* **Notifications** for chats set to `on` go out with `port.notify(..., origin=address,
  code="incoming")` after the submits, so the owner's reply can be relayed back.
* **Errors.** One unreadable chat does not stop the others. A reading error is sent to the owner
  (`code="wechat_error"`) at most once an hour per distinct text. A new message shard without a key
  stops reading with such an error until `key_setup` is run again.

**Readiness.** The desktop helper reports `ready`, `logged_out`, `confirming`, `unavailable` or
`identity_unknown`. The transport turns `ready` into `identity_unknown` unless the helper's account ID
equals the reader's account folder name (`wxid_…` above `db_storage`). The helper derives that ID from
the single `wxid_*` folder under `~/Documents/xwechat_files` when the client does not expose its own
account name; with an exposed name it reports `desktop:<hash>`, which never matches. Changes go to
`port.set_ready` (deliveries wait while not ready); a new reason while still not ready is a
`wechat_state` event; leaving `ready` for `logged_out` or `identity_unknown` calls `port.cancel_all`.
`$wechat_login` opens a 180-second window in which polling clicks the client's confirm button.

**Delivering.** `deliver(reply)` suppresses `progress` and `event`, renders notices, and for an owner
send (`reply.source == "owner"`) first cancels the chat's open runs. Each text part then goes through:

1. **Spacing** (`_slot`): wait without the lock until `account_interval_s` and the chat's interval have
   passed, then take the account's send slot. Owner sends skip this.
2. **`port.check(reply)`**: stop if the run closed or the conversation is no longer admitted.
3. **Checked send** (`transport.send_checked`): the transport refuses a display name shared by two
   chats; the helper selects the chat and returns the account state; the transport re-reads the chat from the
   database. `prepare` refuses if the account changed or a manual reply appeared since the cursor
   (and cancels the conversation's runs), and calls `port.check` again. Only then does the transport
   write `commit`; the helper re-verifies account, recipient, empty draft and quote target, fills the
   editor, checks it, and clicks Send once.
4. **Receipt**: the transport waits for exactly one new outgoing database row with that text (3 s; for
   files up to 600 s and a committed server ID). That row's ID is the `Delivery.message_id`.

A send that has begun runs to the end even if its run is cancelled (`asyncio.shield`), and its receipt
is still recorded as the bot's own. Errors before Send, or helper errors saying nothing was sent, are
`SendFailed` (`Delivery(ok=False)`); anything after the commit is uncertain: `Delivery(ok=False,
uncertain=True)`, an owner event `send_unverified`, and no retry. If the row shows up within 15 minutes
it is recorded as the bot's reply, not as a manual one. Files are copied into a fresh private outbox
folder first; the helper accepts only paths inside the outbox.

**Safety.**

* *Database access* is read-only: files opened `O_RDONLY | O_NOFOLLOW`, shared SQLite locks held only
  while copying, only committed WAL frames used, every page authenticated with HMAC-SHA512 before
  decryption, a 256 MiB bound, and the plaintext only in an in-memory `query_only` connection.
  Reads run on one `wechat-reader` thread so the event loop never blocks.
* *Key files* are rejected unless they are regular files with one hard link, not symlinks,
  0600, owned by the bot user, for this account folder, and cover contact, session and the first
  message database; each key must match its database's salt.
* *Parsing*: XML with `DOCTYPE` or `ENTITY` is refused; compressed fields are bounded to 4 MiB; a
  schema the reader does not know fails closed.
* *Desktop control* clicks only after checking that the window under the pointer belongs to the
  WeChat process, and refuses ambiguous controls, unsent drafts and existing quote drafts.
* *Permissions*: reading the owner's WeChat is `private`, so models get it only in conversations private
  to the owner; `wechat.send` is critical, grantable per chat; `login` and `configure` are `never_auto`.
  Approvals for anything a contact triggers go to the control route; contacts cannot approve. See
  [ARCHITECTURE.md, Permissions](../../docs/core/ARCHITECTURE.md#permissions).

## Data it keeps

| Where | What | Retention |
| --- | --- | --- |
| Bottle `cursor:<native id>` | Last handled `[sort_seq, local_id, timestamp]` per chat | Kept |
| Bottle `own` | IDs of the bot's verified sends | Last 300 |
| Bottle `quotes` | Group triggers (ID, text, sender, server ID, kind) for quoting replies | Last 300 |
| Bottle `notify`, `send_intervals`, `paused` | Owner settings from `$wechat_notify`, `wechat.configure`, `$wechat_pause` | Kept |
| `<data>/keys.json`, `<data>/image_key.json` | Key files (default paths), 0600 | Until you delete them |
| `<data>/images/` | Decrypted copies of received images in admitted chats, named by the `.dat` file | Not pruned by the module |
| `<data>/outbox/<hex>/` | Private copies of files being sent (0700 folders, 0600 files) | Folders older than a day are removed at the next file send |
| `<data>/login/` | Login QR screenshots (0600) | Not pruned by the module |

Message text goes into the core's conversation history through `submit` and `observe`, not into the
module's own storage. Decrypted databases exist only in memory.

## Tests

[tests/test_core_wechat.py](../../tests/test_core_wechat.py) runs the module in a real core with a
`FakeTransport` that scripts chats, messages, account states and send outcomes:

* Reading: first-start baseline, restart catch-up without replay, group mentions and stripping,
  recorded chatter, quotes, contacts' `$commands`, unadmitted chats, media and images, burst
  gathering, busy chats, manual replies cancelling runs.
* Delivery: group quotes and long-text splitting, files, hidden failures and events, a manual reply
  during delivery, unverified sends and late receipts, per-chat and per-account spacing.
* Owner operations: list/status/read/send (including the `chat` plus `spec` refusal), login and
  confirmation, readiness events and sign-out cancellation, notifications and relayed replies,
  pause/resume.
* The documented policy end to end with the `wechat` persona and `lane: sender`.
* Dormancy, V2 image decryption, snapshot decryption and tampering, key file checks, the transport's
  two-phase protocol against a scripted helper, error classification, text splitting, environment
  fallbacks and the native reader's mention parsing.

```bash
venv/bin/python -m unittest tests.test_core_wechat
```

The desktop helper and the key recovery tools need a live client and are not covered.

## Building something similar

* **Stay dormant until configured** ([extension.py:63-67](extension.py)): a drop-in every install
  loads should contribute nothing without settings.
* **Put the account in the address** ([interface.py:151-158](interface.py)): `wxid_me/…` keeps policy
  for one account from applying to another, and `native_id` rejects addresses of other accounts.
* **Advance the cursor only after `submit`, one message at a time** ([interface.py:296-303](interface.py)),
  and treat the very first start as history ([interface.py:257-262](interface.py)). See
  [EXTENSIONS.md, Your bottle](../../docs/core/EXTENSIONS.md#your-bottle).
* **Report facts, not policy** ([interface.py:310-346](interface.py)): `mentions_me` from platform
  metadata, quotes as `reply_to`, unaddressed context through `observe`. Admission decides the rest.
* **Keep blocking work off the event loop**: one worker thread for a non-thread-safe reader
  ([transport.py:56-58](transport.py)); desktop automation in a subprocess with its own interpreter
  and a JSON-only protocol ([transport.py:117-144](transport.py)).
* **Check right before the irreversible step**: wait for spacing without the lock
  ([interface.py:452-472](interface.py)), then `port.check` inside the two-phase send
  ([interface.py:488-500](interface.py), [transport.py:146-196](transport.py)).
* **Never retry what may have been sent** ([transport.py:38-44](transport.py),
  [interface.py:552-559](interface.py)): classify failures as not sent or uncertain, tell the owner,
  and recognise a late receipt. Shield a send once started ([interface.py:542-550](interface.py)).
* **Prove delivery from the platform's own record** ([transport.py:295-334](transport.py)), not from
  the UI saying the button was clicked.
* **Let the subject be what is sent** ([interface.py:642-656](interface.py)): `wechat.send` refuses
  `chat` together with `spec`, so an approval or grant for one chat cannot send to another.
* **Interface-only settings in the bottle**, edited by operations (`notify`, `send_intervals`), and
  core policy read with `port.policy` for status ([interface.py:583-600](interface.py)).

Pitfalls: shipping no admission default means the core's `*` rule applies; text matching for mentions
or recipients (use IDs, refuse ambiguous names); a reading loop that stops on one bad chat; reporting
a send as failed when it may have gone out.

See also: [docs/WECHAT.md](../../docs/WECHAT.md) (user guide),
[docs/core/EXTENSIONS.md](../../docs/core/EXTENSIONS.md) (interfaces, the port, operations),
[docs/core/ARCHITECTURE.md](../../docs/core/ARCHITECTURE.md) (permissions, interface reports),
[plugins/webui](../webui/interface.py) and [extensions/discord](../../extensions/discord/interface.py)
(other interfaces), [plugins/README.md](../README.md).
