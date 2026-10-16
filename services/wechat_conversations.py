"""Durable per-chat takeover settings and incoming-only WeChat reply workers.

The desktop transport belongs to the account, not to an individual conversation.
Enabled workers use the shared agent loop and tool registry with conversation-scoped delivery.
"""

import asyncio
from collections import defaultdict
from contextlib import suppress
import hashlib
import inspect
import json
import time
import re
from services.request_timing import RequestTiming, current_timing

from services import database as db
from app.bot_types import BotResponse
from services.wechat_agent import ConversationChanged


class WeChatConversationStore:
    def native_bindings(self, account_id):
        with db.get_db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS wechat_native_bindings (account_id TEXT NOT NULL, chat_id TEXT NOT NULL, native_id TEXT NOT NULL, PRIMARY KEY(account_id,chat_id), UNIQUE(account_id,native_id))')
            return {row['chat_id']:row['native_id'] for row in conn.execute(
                'SELECT chat_id,native_id FROM wechat_native_bindings WHERE account_id=?', (account_id,))}

    def pin_native_chat(self, account_id, chat_id, native_id):
        previous = self.native_bindings(account_id).get(chat_id)
        if previous and previous != native_id:
            raise ValueError('The native identity of this whitelisted chat cannot be reassigned')
        with db.get_db() as conn:
            conn.execute('INSERT INTO wechat_native_bindings VALUES (?,?,?) ON CONFLICT(account_id,chat_id) DO NOTHING',
                         (account_id,chat_id,native_id))
        return self.native_bindings(account_id)

    def _ensure_tables(self):
        from pathlib import Path
        path = Path(db.DATABASE_PATH).resolve()
        info = path.stat() if path.exists() else None
        identity = (str(path),info.st_dev,info.st_ino) if info else None
        if identity is not None and identity == getattr(self,'_schema_identity',None):
            return
        with db.get_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS wechat_conversations (
                account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                agent_enabled INTEGER NOT NULL DEFAULT 0,
                instructions TEXT NOT NULL DEFAULT '', delay_seconds REAL NOT NULL DEFAULT 20,
                max_reply_chars INTEGER NOT NULL DEFAULT 500, revision INTEGER NOT NULL DEFAULT 0,
                notify_channel TEXT, notify_mode TEXT NOT NULL DEFAULT 'inherit',
                PRIMARY KEY (account_id, chat_id))""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(wechat_conversations)").fetchall()}
            if "notify_mode" not in columns:
                conn.execute("ALTER TABLE wechat_conversations ADD COLUMN notify_mode TEXT NOT NULL DEFAULT 'inherit'")
            for name, definition in {'whitelisted':'INTEGER NOT NULL DEFAULT 0',
                                     'chat_kind':"TEXT NOT NULL DEFAULT 'unknown'",
                                     'mention_aliases':"TEXT NOT NULL DEFAULT '[]'"}.items():
                if name not in columns:
                    conn.execute(f'ALTER TABLE wechat_conversations ADD COLUMN {name} {definition}')
            conn.execute("""CREATE TABLE IF NOT EXISTS wechat_conversation_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
                payload TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'observed',
                recorded_at REAL NOT NULL,
                UNIQUE(account_id, chat_id, message_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS wechat_read_cursors (
                account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                sort_seq INTEGER NOT NULL, local_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY (account_id, chat_id))""")
        info = path.stat()
        self._schema_identity = (str(path),info.st_dev,info.st_ino)

    def get(self, account_id, chat_id):
        self._ensure_tables()
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM wechat_conversations WHERE account_id=? AND chat_id=?",
                               (str(account_id), str(chat_id))).fetchone()
            if row is None:
                conn.execute("INSERT OR IGNORE INTO wechat_conversations (account_id,chat_id) VALUES (?,?)",
                             (str(account_id), str(chat_id)))
                row = conn.execute("SELECT * FROM wechat_conversations WHERE account_id=? AND chat_id=?",
                                   (str(account_id),str(chat_id))).fetchone()
        result = dict(row)
        result["agent_enabled"] = bool(result["agent_enabled"])
        result['whitelisted'] = bool(result['whitelisted'])
        result['mention_aliases'] = json.loads(result['mention_aliases'])
        return result

    def configure(self, account_id, chat_id, **settings):
        allowed = {"agent_enabled", "instructions", "delay_seconds", "max_reply_chars", "notify_channel", "notify_mode",
                   'whitelisted', 'chat_kind', 'mention_aliases'}
        if set(settings) - allowed:
            raise ValueError("Unknown WeChat conversation setting")
        current = self.get(account_id, chat_id)
        if "agent_enabled" in settings and type(settings["agent_enabled"]) is not bool:
            raise ValueError("agent_enabled must be a boolean")
        if 'whitelisted' in settings and type(settings['whitelisted']) is not bool:
            raise ValueError('whitelisted must be a boolean')
        if 'chat_kind' in settings and settings['chat_kind'] not in {'group','direct','unknown'}:
            raise ValueError('chat_kind must be group, direct or unknown')
        if 'mention_aliases' in settings:
            aliases = settings['mention_aliases']
            if not isinstance(aliases, list) or len(aliases) > 10 or any(not isinstance(a,str) or not a.strip() or len(a)>128 for a in aliases):
                raise ValueError('Use up to ten nonempty exact mention names, each at most 128 characters')
            aliases=[a.strip().removeprefix('@').replace('\ufe0f','') for a in aliases]
            if any(not a or '\n' in a or '\r' in a for a in aliases):
                raise ValueError('Mention names must be nonempty single-line nicknames')
            settings['mention_aliases'] = list(dict.fromkeys(aliases))
        if "instructions" in settings:
            if not isinstance(settings["instructions"], str) or len(settings["instructions"]) > 4000:
                raise ValueError("Instructions must be text of at most 4000 characters")
        if "delay_seconds" in settings:
            value = settings["delay_seconds"]
            if type(value) not in (float, int) or not 5 <= value <= 3600:
                raise ValueError("Reply delay must be between 5 and 3600 seconds")
        if "max_reply_chars" in settings:
            value = settings["max_reply_chars"]
            if type(value) is not int or not 20 <= value <= 2000:
                raise ValueError("Reply length must be between 20 and 2000 characters")
        if "notify_channel" in settings:
            value = settings["notify_channel"]
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                raise ValueError("Notification destination must be a channel ID or null")
        if "notify_mode" in settings and settings["notify_mode"] not in ("inherit", "on", "off"):
            raise ValueError("Notification mode must be inherit, on, or off")
        changed = {key: value for key, value in settings.items() if current[key] != value}
        if changed:
            with db.get_db() as conn:
                assignments = ",".join(key + "=?" for key in changed)
                conn.execute("UPDATE wechat_conversations SET " + assignments +
                             ",revision=revision+1 WHERE account_id=? AND chat_id=?",
                             (*(json.dumps(value, ensure_ascii=False) if key=='mention_aliases' else value
                                for key,value in changed.items()), str(account_id), str(chat_id)))
        return self.get(account_id, chat_id)

    def mention_names(self, account_id, chat_id):
        return self.get(account_id, chat_id)['mention_aliases'] or self.get(account_id, '*')['mention_aliases']

    def reply_ready(self, account_id, chat_id):
        settings = self.get(account_id, chat_id)
        return (settings['whitelisted'] and settings['agent_enabled'] and settings['chat_kind'] in {'direct','group'}
                and (settings['chat_kind']=='direct' or getattr(self,'native_mentions',False)
                     or bool(self.mention_names(account_id, chat_id))))

    def addressed(self, account_id, chat_id, message):
        return self.reply_gate(account_id, chat_id, message) == 'accepted'

    def reply_gate(self, account_id, chat_id, message):
        """Explain the same policy used by generation and pre-send checks."""
        settings = self.get(account_id, chat_id)
        if not settings['whitelisted']:
            return 'not_whitelisted'
        if not settings['agent_enabled']:
            return 'assistant_disabled'
        if settings['chat_kind'] not in {'direct', 'group'}:
            return 'chat_kind_unknown'
        if settings['chat_kind']=='group' and getattr(self,'native_mentions',False):
            return 'accepted' if message.get('mentions_me') is True else 'other_mention'
        if settings['chat_kind'] == 'group' and not self.mention_names(account_id, chat_id):
            return 'mention_name_unconfigured'
        if settings['chat_kind']=='direct' and message.get('chat_kind')!='group':
            return 'accepted'
        aliases = self.mention_names(account_id, chat_id)
        if message.get('mentions_me') is True:
            return 'accepted'
        if message.get('mentions_me') is False:
            return 'other_mention'
        names = message.get('mention_names')
        if names is not None:
            matched = any(alias in [name.replace('\ufe0f','') for name in names] for alias in aliases)
            return 'accepted' if matched else 'mention_name_mismatch' if names else 'not_mentioned'
        text = str(message.get('text') or '').replace('\ufe0f','')
        matched = any(re.search(r'(?<![\w@])@' + re.escape(alias) + r'(?=$|[\s:：,，!！?？])', text) for alias in aliases)
        return 'accepted' if matched else 'mention_name_mismatch' if '@' in text else 'not_mentioned'

    def list_settings(self, account_id):
        self._ensure_tables()
        with db.get_db() as conn:
            rows = conn.execute("SELECT chat_id FROM wechat_conversations WHERE account_id=? ORDER BY chat_id",
                                (str(account_id),)).fetchall()
        return [self.get(account_id, row["chat_id"]) for row in rows]

    @staticmethod
    def message_id(message):
        identifier = message.get("id")
        if identifier:
            return str(identifier)
        # A UI transport should provide stable row IDs. This fallback cannot
        # distinguish identical messages without timestamps or row identities.
        fields = {key: message.get(key) for key in
                  ("chat_id", "sender_id", "sender_name", "text", "timestamp")}
        return "fingerprint:" + hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()

    def record(self, account_id, chat_id, message, source="observed"):
        self._ensure_tables()
        payload = {key: message.get(key) for key in
                   ("id", "chat_id", "sender_id", "sender_name", "text", "outgoing", "kind", "timestamp", 'chat_kind', 'mention_names', 'mentions_me',
                    'position','server_id','reply_to_id','reply_to_sender_id')}
        payload["text"] = str(payload.get("text") or "")[:4000]
        payload["id"] = self.message_id(message)
        payload["chat_id"] = str(chat_id)
        # Missing/unknown direction must remain unknown, not become incoming.
        payload["outgoing"] = payload.get("outgoing") if type(payload.get("outgoing")) is bool else None
        payload["kind"] = payload.get("kind") or "text"
        with db.get_db() as conn:
            if conn.execute('SELECT 1 FROM wechat_conversation_messages WHERE account_id=? AND chat_id=? AND message_id=?',
                            (str(account_id),str(chat_id),payload['id'])).fetchone():
                return False
            cursor = conn.execute("""INSERT OR IGNORE INTO wechat_conversation_messages
                (account_id,chat_id,message_id,payload,source,recorded_at) VALUES (?,?,?,?,?,?)""",
                (str(account_id), str(chat_id), payload["id"], json.dumps(payload, ensure_ascii=False, default=str),
                 source, time.time()))
            inserted = cursor.rowcount == 1
            conn.execute("""DELETE FROM wechat_conversation_messages WHERE account_id=? AND chat_id=?
                AND sequence NOT IN (SELECT sequence FROM wechat_conversation_messages
                    WHERE account_id=? AND chat_id=? ORDER BY sequence DESC LIMIT 400)""",
                (str(account_id), str(chat_id), str(account_id), str(chat_id)))
        return inserted

    def read_cursor(self, account_id, chat_id):
        """Return the last observed native position and the settings revision it was observed under."""
        self._ensure_tables()
        with db.get_db() as conn:
            row = conn.execute('SELECT sort_seq,local_id,revision FROM wechat_read_cursors WHERE account_id=? AND chat_id=?',
                               (str(account_id), str(chat_id))).fetchone()
        return ((row['sort_seq'], row['local_id']), row['revision']) if row else None

    def save_read_cursor(self, account_id, chat_id, position, revision):
        self._ensure_tables()
        with db.get_db() as conn:
            conn.execute("""INSERT INTO wechat_read_cursors VALUES (?,?,?,?,?,?)
                ON CONFLICT(account_id,chat_id) DO UPDATE SET sort_seq=excluded.sort_seq,
                local_id=excluded.local_id, revision=excluded.revision, updated_at=excluded.updated_at""",
                (str(account_id), str(chat_id), int(position[0]), int(position[1]), int(revision), time.time()))

    def history(self, account_id, chat_id, limit=30):
        self._ensure_tables()
        with db.get_db() as conn:
            rows = conn.execute("""SELECT payload FROM wechat_conversation_messages WHERE account_id=? AND chat_id=?
                ORDER BY sequence DESC LIMIT ?""", (str(account_id), str(chat_id), max(1, min(int(limit), 50)))).fetchall()
        result = []
        budget = 0
        for row in rows:
            message = json.loads(row["payload"])
            cost = len(row["payload"])
            if budget + cost > 12000:
                break
            result.append(message)
            budget += cost
        return list(reversed(result))

    def is_agent_echo(self, account_id, chat_id, message):
        if not message.get("outgoing"):
            return False
        self._ensure_tables()
        with db.get_db() as conn:
            rows = conn.execute("""SELECT payload FROM wechat_conversation_messages
                WHERE account_id=? AND chat_id=? AND source='agent' AND recorded_at>?""",
                (str(account_id), str(chat_id), time.time() - 120)).fetchall()
        if str(message.get('id','')).startswith('native:'):
            return any(json.loads(row['payload']).get('id') == message['id'] for row in rows)
        return any(json.loads(row["payload"]).get("text") == message.get("text") for row in rows)


class WeChatReplySupervisor:
    def __init__(self, transport, store, notify=None, generator=None, account_interval_seconds=20,
                 coalesce_seconds=1, catch_up_seconds=900):
        self.transport, self.store, self.notify = transport, store, notify
        if generator is None:
            from providers.wechat_reply import WeChatReplyGenerator
            generator = WeChatReplyGenerator()
        self.generator = generator
        self.account_interval_seconds = max(5.0, float(account_interval_seconds))
        self.coalesce_seconds = max(0.0, min(5.0, float(coalesce_seconds)))
        self.catch_up_seconds = max(0.0, min(3600.0, float(catch_up_seconds)))
        self._baselines = set()
        self._saved_cursors = {}
        self._revisions = {}
        self._epochs = defaultdict(int)
        self._pending = defaultdict(list)
        self._tasks = {}
        self._account_locks = defaultdict(asyncio.Lock)
        self._last_account_send = defaultdict(float)
        self._last_chat_send = defaultdict(float)
        self._closed = False
        self._errors = {}
        self._sending = set()
        self._sleep = asyncio.sleep
        self.agent = None
        self._resuming = set()
        self._routing = {}
        self._positions = {}
        self._group_requests = {}

    @staticmethod
    def _position(message):
        value = message.get('position')
        return tuple(value) if isinstance(value,(list,tuple)) and len(value)==2 and all(type(v) is int for v in value) else None

    async def _notify(self, key, event_type, message):
        if self.notify is None:
            return
        settings = self.store.get(*key)
        try:
            await self.notify({"account_id": key[0], "chat_id": key[1], "type": event_type,
                               "message": message, "notify_channel": settings["notify_channel"]})
        except Exception as error:
            # A failed Discord delivery must not restart or duplicate a WeChat send.
            print(f"[wechat reply] notification failed: {type(error).__name__}")

    def _catch_up_floor(self, key, revision):
        """A durable cursor from the same settings revision marks where observation stopped."""
        if self.catch_up_seconds <= 0:
            return None
        saved = self.store.read_cursor(*key)
        if saved is None or saved[1] != revision:
            return None
        return saved[0]

    def _recent(self, message):
        timestamp = message.get('timestamp')
        return type(timestamp) in (int, float) and time.time() - timestamp <= self.catch_up_seconds

    def _save_cursor(self, key):
        position, revision = self._positions.get(key), self._revisions.get(key)
        if position is None or revision is None or self._saved_cursors.get(key) == (position, revision):
            return
        self.store.save_read_cursor(*key, position, revision)
        self._saved_cursors[key] = (position, revision)

    async def baseline(self, account_id, chat_id, messages):
        """Treat visible history as already handled, except recent messages never observed before a restart.

        Returns the unobserved messages left for the normal observe() path to route.
        """
        key = (str(account_id), str(chat_id))
        await self._invalidate(key)
        revision = self.store.get(*key)["revision"]
        floor = self._catch_up_floor(key, revision)
        catch_up, positions = [], []
        for message in messages:
            position = self._position(message)
            if floor is not None and position is not None and position > floor and self._recent(message):
                catch_up.append(message)
                continue
            self.store.record(*key, message)
            if position is not None:
                positions.append(position)
        if catch_up:
            self._positions[key] = floor
            print('[wechat bridge] ' + json.dumps({'chat_id': key[1], 'catch_up_messages': len(catch_up)}))
        elif positions:
            self._positions[key] = max(positions)
        self._baselines.add(key)
        self._revisions[key] = revision
        self._save_cursor(key)
        return catch_up

    async def reset_baseline(self, account_id=None, chat_id=None):
        keys = set(self._baselines) | set(self._tasks)
        for key in keys:
            if (account_id is None or key[0] == str(account_id)) and (chat_id is None or key[1] == str(chat_id)):
                await self._invalidate(key)
                self._baselines.discard(key)
                self._revisions.pop(key, None)
                self._positions.pop(key, None)

    async def _invalidate(self, key, reason='conversation_changed'):
        self._epochs[key] += 1
        self._pending.pop(key, None)
        self._resuming.discard(key)
        self._group_requests.pop(key,None)
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            print('[wechat cancellation] '+json.dumps({'chat_id':key[1],'reason':reason}))
            timing = current_timing.get()
            if timing is not None:
                timing.record('wechat_reply_cancelled',reason=reason)
        if task is not None and task is not asyncio.current_task() and not task.done():
            # Cancelling a GUI click cannot undo it. Let an already started send
            # finish and retain its receipt instead of creating an ambiguous retry.
            if key not in self._sending:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.agent is not None:
            self.agent.reset(key)

    def resume(self, account_id, chat_id):
        key = (str(account_id), str(chat_id))
        if self.agent is None or self._closed or not self.store.reply_ready(*key):
            raise ValueError('No enabled conversation worker is available')
        self._resuming.add(key)
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._run(key))

    async def configure(self, account_id, chat_id, **settings):
        key = (str(account_id), str(chat_id))
        previous = self.store.get(*key)
        current = self.store.configure(*key, **settings)
        if previous["revision"] != current["revision"]:
            await self._invalidate(key,reason='settings_changed')
            self._baselines.discard(key)
            self._revisions[key] = current["revision"]
        return current

    async def manual_sent(self, account_id, chat_id):
        key = (str(account_id), str(chat_id))
        await self._invalidate(key,reason='manual_reply')
        self._last_chat_send[key] = time.monotonic()
        self._last_account_send[key[0]] = time.monotonic()

    async def observe(self, account_id, chat_id, messages):
        if self._closed:
            return
        key = (str(account_id), str(chat_id))
        settings = self.store.get(*key)
        if key not in self._baselines or self._revisions.get(key) != settings["revision"]:
            if not await self.baseline(*key, messages):
                return
        fresh = []
        notifications = []
        manual = False
        uncertain = False
        floor = self._positions.get(key)
        for message in messages:
            position = self._position(message)
            if position is not None:
                if floor is not None and position <= floor:
                    continue
                self._positions[key] = max(position,self._positions.get(key,position))
            echo = (self.store.is_agent_echo(*key, message)
                    or (message.get('outgoing') is True and message.get('id') in getattr(self.transport, 'own_message_ids', ())))
            if not self.store.record(*key, message):
                continue
            if echo:
                # Verified outgoing media is our own delivery, not unsupported
                # activity that should cancel the worker's completion message.
                continue
            if (message.get('outgoing') is False and message.get('kind', 'text') == 'text'
                    and str(message.get('text') or '').strip()):
                notifications.append(dict(message))
            if message.get("kind", 'text') != 'text' or type(message.get("outgoing")) is not bool:
                if (settings['chat_kind']=='group' and message.get('outgoing') is False
                        and not self.store.addressed(*key,message)):
                    continue
                uncertain = True
                continue
            if message.get("outgoing"):
                if not echo:
                    manual = True
                    # Keep any later incoming message in this chronological
                    # batch: an older manual reply cannot answer a future turn.
                    fresh = []
            elif message.get("kind", "text") == "text" and str(message.get("text") or "").strip():
                fresh.append(dict(message))
        self._save_cursor(key)
        for message in notifications:
            await self._notify(key, "incoming", message)
        if notifications:
            decisions = defaultdict(int)
            for message in notifications:
                decisions[self.store.reply_gate(*key, message)] += 1
            self._routing[key] = {'observed_at': time.time(), 'decisions': dict(decisions),
                                  'unsupported_activity': uncertain}
            print('[wechat routing] ' + json.dumps({'chat_id': key[1], **self._routing[key]}))
        if uncertain:
            had_work = bool(self._pending.get(key)) or key in self._tasks
            await self._invalidate(key,reason='unsupported_activity')
            if settings["agent_enabled"] or had_work:
                self._errors[key] = "unsupported_activity"
                await self._notify(key, "reply_skipped",
                    "New WeChat activity could not be classified reliably. The pending reply was cancelled; nothing new was sent.")
            return
        if manual:
            await self.manual_sent(*key)
        fresh = [m for m in fresh if self.store.addressed(*key, m)]
        if not fresh:
            return
        self._pending[key].extend(fresh)
        self._pending[key] = self._pending[key][-30:]
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._run(key))

    def _valid(self, key, revision, epoch):
        settings = self.store.get(*key)
        return (not self._closed and self.store.reply_ready(*key) and settings["revision"] == revision
                and self._epochs[key] == epoch)

    def _defer_newer_messages(self, key, generating_messages):
        """Regenerate one combined response if the conversation moved ahead."""
        if self.store.get(*key)['chat_kind']=='group':
            return False
        newer = self._pending.get(key)
        if not newer:
            return False
        combined, seen = [], set()
        for message in [*generating_messages, *newer]:
            identifier = self.store.message_id(message)
            if identifier not in seen:
                seen.add(identifier)
                combined.append(message)
        self._pending[key] = combined[-30:]
        return True

    async def _run(self, key):
        timing = RequestTiming()
        timing_token = current_timing.set(timing)
        timing.record('wechat_worker_started', platform='wechat', chat_id=key[1])
        try:
            while (self._pending.get(key) or key in self._resuming) and not self._closed:
                settings = self.store.get(*key)
                revision, epoch = settings["revision"], self._epochs[key]
                # Generation can overlap the outbound cooldown. Only a short
                # gathering window is needed to combine incoming bursts.
                delay = 0 if key in self._resuming else self.coalesce_seconds
                timing.record('wechat_coalescing', planned_wait_s=delay)
                await self._sleep(delay)
                if not self._valid(key, revision, epoch):
                    return
                resuming = key in self._resuming
                self._resuming.discard(key)
                if settings['chat_kind']=='group':
                    current = self._group_requests.get(key)
                    waiting = self.agent.pending(key) if self.agent is not None and current else None
                    if not resuming and waiting and waiting['status']=='awaiting_confirmation':
                        break
                    queue = self._pending.get(key,[])
                    if resuming:
                        pending = []
                    elif waiting and waiting['status']=='needs_input':
                        followup = next((item for item in queue if item.get('sender_id')==current['target'].get('sender_id')),None)
                        if followup is None:
                            break
                        queue.remove(followup)
                        pending = [followup]
                        current['target'] = followup
                    else:
                        pending = queue[:1]
                        if pending:
                            del queue[:1]
                            current = {'target':pending[0],'request_id':pending[0]['id']}
                            self._group_requests[key] = current
                    if not queue:
                        self._pending.pop(key,None)
                else:
                    pending = self._pending.pop(key, [])
                if not pending and not resuming:
                    return
                timing.record('wechat_generation_started')
                context = {"account_id": key[0], "chat_id": key[1], "settings": settings,
                           "history": self.store.history(*key), "messages": pending, "epoch": epoch}
                if settings['chat_kind']=='group':
                    target = current['target'] if current else None
                    context['reply_target'] = target
                    context['reply_request_id'] = current['request_id'] if current else None
                    sender = target.get('sender_id') if target else None
                    context['history']=[m for m in context['history'] if sender and (
                        (m.get('outgoing') is True and m.get('reply_to_sender_id')==sender)
                        or (m.get('outgoing') is False and m.get('sender_id')==sender and self.store.addressed(*key,m)))]
                if self.agent is not None:
                    async def publish(response):
                        delivery_pending = pending or ([context['reply_target']] if context.get('reply_target') else [])
                        receipt = await self._deliver(key, revision, epoch, delivery_pending, response)
                        if receipt is None or receipt.get('deferred'):
                            raise ConversationChanged()
                        if not receipt.get('verified'):
                            raise RuntimeError('Message or file delivery could not be verified')
                    try:
                        await self.agent.run(context, publish,
                            lambda: self._valid(key, revision, epoch) and
                            (settings['chat_kind']=='group' or not self._pending.get(key)))
                    except ConversationChanged:
                        if not self._valid(key, revision, epoch):
                            return
                        self._defer_newer_messages(key, pending)
                    continue
                reply = await self.generator(context)
                if not self._valid(key, revision, epoch):
                    return
                if self._defer_newer_messages(key, pending):
                    continue
                if not isinstance(reply, str) or not reply.strip():
                    self._errors[key] = "generation_failed_or_skipped"
                    await self._notify(key, "reply_skipped", "No reliable reply was generated; nothing was sent.")
                    continue
                reply = reply.strip()
                if len(reply) > settings["max_reply_chars"]:
                    self._errors[key] = "reply_too_long"
                    await self._notify(key, "reply_skipped", "Generated reply exceeded this chat's limit; nothing was sent.")
                    continue
                receipt = await self._deliver(key, revision, epoch, pending, BotResponse(text=reply))
                if receipt is None:
                    return
                if receipt.get('deferred'):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._errors[key] = type(error).__name__
            print('[wechat reply] '+json.dumps({'chat_id':key[1], 'error':type(error).__name__,
                                               'detail':str(error)[:400]},ensure_ascii=False))
            await self._notify(key, "reply_failed", "Reply processing stopped; nothing will be retried automatically.")
        finally:
            # observe/manual_sent may already have installed a replacement task.
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
            timing.record('wechat_worker_finished', error_code=self._errors.get(key))
            current_timing.reset(timing_token)

    async def _deliver(self, key, revision, epoch, pending, response):
        text = (response.text or '').strip()
        settings = self.store.get(*key)
        if text and len(text) > settings['max_reply_chars']:
            raise ValueError('A spoken update exceeds this chat’s message limit')
        queued_at = time.monotonic()
        async with self._account_locks[key[0]]:
            now = time.monotonic()
            wait = max(0, self._last_account_send[key[0]] + self.account_interval_seconds - now,
                       self._last_chat_send[key] + settings['delay_seconds'] - now)
            timing = current_timing.get()
            if timing is not None:
                timing.record('wechat_delivery_wait', queue_wait_s=round(now-queued_at, 3), planned_spacing_wait_s=round(wait, 3))
            await self._sleep(max(0, wait))
            if not self._valid(key, revision, epoch):
                return None
            if timing is not None:
                timing.record('wechat_delivery_checks_started')
            account = None
            async def prepare(current_account, latest):
                nonlocal account
                account = current_account
                if account.get('state') != 'ready' or str(account.get('account_id')) != key[0]:
                    self._errors[key] = 'account_not_ready'
                    await self._notify(key, 'reply_skipped', 'WeChat is not logged in to the expected account.')
                    return {'cancelled': True}
                await self.observe(*key, latest)
                if not self._valid(key, revision, epoch):
                    return {'cancelled': True}
                if self._defer_newer_messages(key, pending):
                    return {'deferred': True}
                return None
            def authorize():
                if not self._valid(key, revision, epoch):
                    raise PermissionError('The whitelist or conversation settings changed before sending')
            def on_commit():
                self._last_account_send[key[0]] = time.monotonic()
                self._last_chat_send[key] = time.monotonic()
                self._sending.add(key)
            try:
                if response.attachment is None and getattr(self.transport, 'atomic_checked_sends', False) is True:
                    reply_options = {'reply_to':pending[0]} if (settings['chat_kind']=='group' and pending
                        and getattr(self.transport,'supports_quoted_replies',False) is True) else {}
                    receipt = await self.transport.send_checked(key[1], text, expected_account=key[0],
                        prepare=prepare, authorize=authorize, on_commit=on_commit, **reply_options)
                    if receipt.get('cancelled'):
                        return None
                    if receipt.get('deferred'):
                        return receipt
                else:
                    account = await self.transport.status()
                    latest = (await self.transport.read_messages(key[1], limit=20)
                              if account.get('state') == 'ready' and str(account.get('account_id')) == key[0] else [])
                    decision = await prepare(account, latest)
                    if decision is not None:
                        return decision if decision.get('deferred') else None
                    on_commit()
                    guard = {'authorize':authorize, 'expected_account':key[0]} if getattr(self.transport,'policy_guarded_sends',False) is True else {}
                    if response.attachment is not None:
                        attachment = response.attachment
                        sending = asyncio.create_task(self.transport.send_file(key[1], attachment.path, attachment.filename, **guard))
                        try:
                            while not sending.done():
                                await asyncio.wait({sending}, timeout=15)
                                if not sending.done() and self.agent is not None:
                                    await self.agent.bridge.work_notice(key, 'progress', 'Waiting for the desktop attachment upload and receipt.')
                            receipt = sending.result()
                        finally:
                            if not sending.done():
                                sending.cancel()
                            await asyncio.gather(sending, return_exceptions=True)
                        text = attachment.filename or attachment.path.rsplit('/', 1)[-1]
                    else:
                        receipt = await self.transport.send(key[1], text, **guard)
            finally:
                self._sending.discard(key)
        if not isinstance(receipt, dict) or not receipt.get('verified'):
            self._errors[key] = 'send_unverified'
            await self._notify(key, 'send_unverified', 'Send could not be verified. It will not be retried automatically.')
            return {'verified': False}
        message = {'id': receipt.get('message_id') or 'agent:' + hashlib.sha256(
                   (text + str(time.time_ns())).encode()).hexdigest(), 'chat_id': key[1],
                   'sender_id': key[0], 'sender_name': account.get('name', key[0]),
                   'text': text, 'outgoing': True, 'kind': 'file' if response.attachment else 'text'}
        if settings['chat_kind']=='group' and pending:
            message.update(reply_to_id=pending[0]['id'],reply_to_sender_id=pending[0].get('sender_id'))
        self.store.record(*key, message, source='agent')
        timing = current_timing.get()
        if timing is not None:
            timing.record('wechat_reply_verified',message_id=message['id'],reply_to_id=message.get('reply_to_id'))
            timing.delivered()
        self._errors.pop(key, None)
        await self._notify(key, 'agent_reply', message)
        return receipt

    def status(self):
        return {"closed": self._closed, "active_chats": [list(key) for key, task in self._tasks.items()
                if not task.done()], "pending_messages": sum(len(items) for items in self._pending.values()),
                'routing': [{'account_id': key[0], 'chat_id': key[1], **decision}
                            for key, decision in self._routing.items()],
                "errors": [{"account_id": key[0], "chat_id": key[1], "code": code}
                           for key, code in self._errors.items()]}

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for key, task in self._tasks.items():
            if key not in self._sending:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending.clear()
        close = getattr(self.generator, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
