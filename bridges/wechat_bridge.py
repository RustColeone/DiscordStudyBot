"""A shared WeChat account with owner controls, notifications and per-chat assistants."""
import asyncio
import json
from contextlib import suppress

from app.bot_types import BotAttachment, BotResponse
from bridges.bridge_base import BridgedObject
from services import database as db
from services.wechat_conversations import WeChatConversationStore, WeChatReplySupervisor
from services.wechat_desktop import WeChatDesktopTransport


class WeChatBridge(BridgedObject):
    def __init__(self, config=None, transport=None, store=None, generator=None):
        super().__init__("wechat-account")
        self.config = config or {}
        if transport is None and self.config.get('WECHAT_BRIDGE_READ_BACKEND','desktop') == 'native':
            from services.wechat_native_transport import WeChatNativeTransport
            transport = WeChatNativeTransport(self.config)
        self.transport = transport or WeChatDesktopTransport(self.config)
        self.store = store or WeChatConversationStore()
        if generator is None:
            from providers.wechat_reply import WeChatReplyGenerator
            generator = WeChatReplyGenerator(self.config)
        self.supervisor = WeChatReplySupervisor(self.transport, self.store, self._notify, generator,
            account_interval_seconds=20,
            coalesce_seconds=self.config.get('WECHAT_BRIDGE_COALESCE_SECONDS', 1))
        self.app = None
        self.poll_task = None
        self.account = {"state": "unavailable", "name": "", "account_id": ""}
        self.last_error = ""
        self._known_account = None
        self._chat_names = {}
        self._initial_all = False
        self._all_previews = {}
        self._active = False
        self._login_until = 0
        self._baselined = set()
        self.upload_limit_mb = max(1, min(100, float(self.config.get('WECHAT_BRIDGE_MAX_UPLOAD_MB', 100))))

    def attach(self, app):
        self.app = app
        if self.supervisor.agent is None and hasattr(app, 'agent_feature'):
            from services.wechat_agent import WeChatConversationAgent
            self.supervisor.agent = WeChatConversationAgent(self, app, self.supervisor.generator)
            if hasattr(app, 'register_message_route'):
                app.register_message_route('wechat:', self.send_scheduled)

    async def send_scheduled(self, channel_id, text):
        if not self._active or self.supervisor.agent is None:
            raise RuntimeError('The WeChat account bridge is paused')
        account_id = await self._account_ready()
        settings = next((s for s in self.store.list_settings(account_id) if self.store.reply_ready(account_id,s['chat_id']) and
                         self.supervisor.agent.channel_id((account_id, s['chat_id'])) == channel_id), None)
        if settings is None:
            raise RuntimeError('The scheduled conversation is no longer enabled for this account')
        key = (account_id, settings['chat_id'])
        receipt = await self.supervisor._deliver(key, settings['revision'], self.supervisor._epochs[key], [], BotResponse(text=text))
        if not receipt or not receipt.get('verified'):
            raise RuntimeError('The scheduled message could not be verified')

    async def work_notice(self, key, kind, text):
        label = self._chat_names.get(key[1], key[1])
        for channel in self._control_channels():
            try:
                await self._send_notice(channel, f'WeChat · {label} · {kind}\n{text}')
            except Exception as error:
                print(f'[wechat work] owner notice failed: {type(error).__name__}')

    @staticmethod
    def _controls():
        with db.get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS wechat_bridge_controls (channel_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL)")
            conn.execute('CREATE TABLE IF NOT EXISTS wechat_forward_routes (channel_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, chat_id TEXT NOT NULL, enabled INTEGER NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS wechat_notification_routes (message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, account_id TEXT NOT NULL, chat_id TEXT NOT NULL)')

    def notification_route(self,message):
        identifier=getattr(message,'metadata',{}).get('referenced_message_id')
        if not identifier or not self._active: return None
        self._controls()
        with db.get_db() as conn:
            row=conn.execute('SELECT * FROM wechat_notification_routes WHERE message_id=? AND channel_id=?',
                             (str(identifier),str(message.channel_id))).fetchone()
        return dict(row) if row else None

    def _forward_route(self, channel_id):
        self._controls()
        with db.get_db() as conn:
            row=conn.execute('SELECT * FROM wechat_forward_routes WHERE channel_id=?',(str(channel_id),)).fetchone()
        return dict(row) if row else None

    def wants_passive(self, channel_id):
        route=self._forward_route(channel_id)
        return bool(self._active and route and route['enabled'])

    async def handle_passive(self, app, message):
        self.attach(app)
        referenced=self.notification_route(message)
        if message.content.startswith('$') or (not referenced and not self.wants_passive(message.channel_id)):
            return []
        owner=str(self.config.get('WECHAT_BRIDGE_OWNER_ID') or self.config.get('CREATOR_USER_ID') or '').strip()
        if str(message.author_id)!=owner:
            return []
        route=referenced or self._forward_route(message.channel_id)
        if not referenced and getattr(message,'metadata',{}).get('referenced_message_id'):
            raise ValueError('This Discord reply is not linked to a recorded WeChat notification. Use --send with --to for an explicit recipient.')
        account_id=await self._account_ready()
        def authorize():
            current=self._forward_route(message.channel_id)
            if referenced:
                if account_id!=referenced['account_id'] or not self.notification_route(message):
                    raise PermissionError('The recorded notification belongs to a different account or is no longer available.')
                return
            if (account_id!=route['account_id'] or not current or not current['enabled']
                    or current['chat_id']!=route['chat_id'] or current['account_id']!=account_id
                    or not self.store.get(account_id,route['chat_id'])['whitelisted']):
                raise PermissionError('Forwarding is paused: account, route, or whitelist changed.')
        authorize()
        await self.supervisor.manual_sent(account_id,route['chat_id'])
        authorize()
        if getattr(self.transport,'policy_guarded_sends',False) is True:
            receipt=await self.transport.send(route['chat_id'],message.content,authorize=authorize,expected_account=account_id)
        else:
            receipt=await self.transport.send(route['chat_id'],message.content)
        if not receipt.get('verified'):
            return [BotResponse(text='Forwarded send could not be verified. Check WeChat before trying again.',metadata={'tool_status':'failed','retryable':False})]
        self.store.record(account_id,route['chat_id'],{'id':receipt.get('message_id'),'text':message.content,'outgoing':True,'kind':'text'},source='manual')
        return [BotResponse(text='Forwarded to WeChat · '+self._chat_names.get(route['chat_id'],route['chat_id']))]

    def _control_channels(self):
        self._controls()
        with db.get_db() as conn:
            return [row[0] for row in conn.execute("SELECT channel_id FROM wechat_bridge_controls WHERE enabled=1")]

    def _control(self, channel_id, enabled=True):
        self._controls()
        with db.get_db() as conn:
            conn.execute("INSERT INTO wechat_bridge_controls VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET enabled=excluded.enabled",
                         (str(channel_id), int(enabled)))

    async def start_if_configured(self):
        if self._control_channels():
            await self.initialize()

    async def initialize(self):
        self._active = True
        if self.poll_task is None or self.poll_task.done():
            print(f"[wechat bridge] reply engine: {getattr(self.supervisor.generator, 'engine_label', 'custom')}")
            self.poll_task = asyncio.create_task(self._poll())
        return True

    async def disconnect(self):
        self._active = False
        self._controls()
        with db.get_db() as conn:
            conn.execute("UPDATE wechat_bridge_controls SET enabled=0")
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        await self.supervisor.reset_baseline()
        return True

    async def close(self):
        # Shutdown keeps durable preferences; a user --disconnect explicitly disables resume.
        self._active = False
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        await self.supervisor.close()
        await self.transport.close()

    async def send_message(self, message):
        raise ValueError("WeChat requires an explicit recipient: $bridge wechat --send \"message\" --to \"name or ID\"")

    def get_status(self):
        status = self.supervisor.status()
        return (f"WeChat: {self.account['state']}\nAccount: {self.account.get('name') or 'not verified'}\n"
                f"Reply engine: {getattr(self.supervisor.generator, 'engine_label', 'custom')}\n"
                f"Message reader: {getattr(self.transport, 'read_backend', 'desktop accessibility')}\n"
                f"Bridge polling: {'on' if self._active else 'off'}\n"
                f"Active reply workers: {len(status['active_chats'])}\n"
                f"Reply whitelist: {sum(s['whitelisted'] for s in self.store.list_settings(self.account.get('account_id','')))} chats\n"
                + (self.account.get("detail", "") or self.last_error))

    def _destination(self, account_id, chat_id):
        settings = self.store.get(account_id, chat_id)
        if settings.get("notify_mode") == "off":
            return None
        if settings.get("notify_mode") == "on":
            return settings.get("notify_channel")
        return self.store.get(account_id, "*").get("notify_channel")

    async def _send_notice(self, channel_id, text):
        if self.app is None or channel_id not in self._control_channels():
            return
        # Received text cannot trigger Discord mentions.
        text=text.replace('@','@\u200b')
        if str(channel_id).startswith('dm:'):
            owner=str(self.config.get('WECHAT_BRIDGE_OWNER_ID') or self.config.get('CREATOR_USER_ID') or '').strip()
            if channel_id!='dm:'+owner:
                raise PermissionError('Notification DMs can only target the configured owner')
            return await self.app.platform.send_direct_message(owner,text)
        else:
            return await self.app.platform.send_channel_message(channel_id,text)

    async def _notify(self, event):
        destination = self._destination(event["account_id"], event["chat_id"])
        if not destination:
            return
        item = event["message"]
        body = f"{item.get('sender_name') or ('Me' if item.get('outgoing') else 'Participant')}: {item.get('text', '')}" if isinstance(item, dict) else str(item)
        label = self._chat_names.get(event["chat_id"], event["chat_id"])
        sent=await self._send_notice(destination, f"WeChat · {label} · {event['type']}\n{body[:1200]}")
        identifier=getattr(sent,'id',None)
        channel_id=getattr(getattr(sent,'channel',None),'id',None)
        if isinstance(identifier,(int,str)) and isinstance(channel_id,(int,str)):
            self._controls()
            with db.get_db() as conn:
                conn.execute('INSERT OR REPLACE INTO wechat_notification_routes VALUES (?,?,?,?)',
                             (str(identifier),str(channel_id),event['account_id'],event['chat_id']))
                conn.execute('DELETE FROM wechat_notification_routes WHERE rowid NOT IN (SELECT rowid FROM wechat_notification_routes ORDER BY rowid DESC LIMIT 2000)')

    async def _account_ready(self):
        self.account = await self.transport.status()
        if self.account.get("state") != "ready" or not self.account.get("account_id"):
            raise ValueError("WeChat account is not ready: " + (self.account.get("detail") or self.account["state"]) + " Use --login.")
        await self._sync_account(self.account["account_id"])
        return self.account["account_id"]

    async def _sync_account(self, account_id):
        if self._known_account == account_id:
            return False
        bind_settings = getattr(self.transport, 'bind_settings', None)
        if callable(bind_settings):
            await bind_settings(self.store, account_id)
        await self.supervisor.reset_baseline()
        self._known_account = account_id
        self._initial_all = False
        self._all_previews = {}
        self._baselined.clear()
        return True

    async def execute(self, command, app, message):
        self.attach(app)
        owner = str(self.config.get("WECHAT_BRIDGE_OWNER_ID") or self.config.get("CREATOR_USER_ID") or "").strip()
        if not owner or str(message.author_id) != owner:
            raise PermissionError("WeChat account commands require the configured owner.")
        action = command.action
        if action == "status":
            self.account = await self.transport.status()
            return [BotResponse(text=self.get_status())]
        if action == "disconnect":
            await self.disconnect()
            return [BotResponse(text="WeChat bridge stopped. Notifications and replies are paused; the desktop account stays signed in.")]
        if action in {"init", "login"}:
            self._control(message.channel_id)
            await self.initialize()
            if action == "login":
                self._login_until = asyncio.get_running_loop().time() + 180
                result = await self.transport.login()
                self.account = result
                if result.get("path"):
                    return [BotResponse(text="Scan this WeChat QR and confirm on your phone. Login is complete only when account status becomes ready. Run --login again if it expires.",
                        attachment=BotAttachment(result["path"], "wechat-login.png", True))]
            self.account = await self.transport.status()
            return [BotResponse(text=self.get_status())]
        account_id = await self._account_ready()
        if action=='whitelist' and command.mode in {'list','defaults'}:
            if command.mode=='defaults' and command.mention_aliases is not None:
                await self.supervisor.configure(account_id,'*',mention_aliases=command.mention_aliases)
                await self.supervisor.reset_baseline(account_id)
                self._baselined.clear()
            defaults=self.store.get(account_id,'*')['mention_aliases']
            allowed=[s for s in self.store.list_settings(account_id) if s['whitelisted']]
            lines=[f"{self._chat_names.get(s['chat_id'],s['chat_id'])} · {s['chat_kind']} · assistant {'on' if s['agent_enabled'] else 'off'} · mentions: {', '.join(self.store.mention_names(account_id,s['chat_id'])) or '(not configured)'}" for s in allowed]
            return [BotResponse(text='WeChat reply whitelist (notifications are configured separately):\n'
                                +'\n'.join(lines or ['(empty)'])+'\nDefault mention names: '+(', '.join(defaults) or '(none)'))]
        if action=='listen' and command.mode in {'off','status'} and not command.target:
            route=self._forward_route(message.channel_id)
            if route and command.mode=='off':
                with db.get_db() as conn: conn.execute('UPDATE wechat_forward_routes SET enabled=0 WHERE channel_id=?',(str(message.channel_id),))
                route['enabled']=0
            return [BotResponse(text='Discord → WeChat forwarding: '+ ('on → '+route['chat_id'] if route and route['enabled'] else 'off'))]
        if action == "chats":
            chats = await self.transport.list_chats()
            self._chat_names.update({c["id"]: c["name"] for c in chats})
            return [BotResponse(text="Available WeChat conversations:\n" + "\n".join(
                f"{c['name']} · ID: {c['id']} · unread: {c.get('unread', 0)}" for c in chats))]
        if action == "notify" and (not command.target or command.target == "all"):
            settings = self.store.get(account_id, "*")
            if command.mode != "status":
                destination='dm:'+owner if command.destination=='dm' else str(message.channel_id)
                settings = self.store.configure(account_id, "*", notify_mode="on" if command.mode == "on" else "off",
                                                notify_channel=destination if command.mode == "on" else None)
                self._initial_all = False
                self._control(message.channel_id)
                if command.mode=='on': self._control(destination)
                await self.initialize()
            return [BotResponse(text=f"WeChat notifications for all exposed chats: {'on' if settings.get('notify_channel') else 'off'}. Destination: {settings.get('notify_channel') or '(none)'}. Independent of the reply whitelist.")]
        chat = await self.transport.resolve(command.target)
        chat_id = chat["id"]
        self._chat_names[chat_id] = chat["name"]
        if action=='whitelist':
            changes={'whitelisted':command.mode=='add'}
            if command.mode=='remove': changes['agent_enabled']=False
            if command.chat_kind is not None: changes['chat_kind']=command.chat_kind
            elif command.mode=='add' and chat.get('chat_kind') in {'group','direct'}: changes['chat_kind']=chat['chat_kind']
            if command.mention_aliases is not None: changes['mention_aliases']=command.mention_aliases
            settings=await self.supervisor.configure(account_id,chat_id,**changes)
            return [BotResponse(text=f"Whitelist {'added' if settings['whitelisted'] else 'removed'}: {chat['name']}. Kind: {settings['chat_kind']}. Group replies require an exact @mention. Use --agent on to start replies.")]
        if action=='listen':
            if command.mode in {'on','toggle'} and not self.store.get(account_id,chat_id)['whitelisted']:
                raise ValueError('Add this conversation to --whitelist before enabling automatic forwarding.')
            previous=self._forward_route(message.channel_id)
            enabled=not(previous and previous['enabled']) if command.mode=='toggle' else command.mode=='on'
            if command.mode!='status':
                self._controls()
                with db.get_db() as conn:
                    conn.execute('INSERT INTO wechat_forward_routes VALUES (?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET account_id=excluded.account_id,chat_id=excluded.chat_id,enabled=excluded.enabled',
                                 (str(message.channel_id),account_id,chat_id,int(enabled)))
                self._control(message.channel_id)
                await self.initialize()
            else: enabled=bool(previous and previous['enabled'])
            return [BotResponse(text=f"Discord → WeChat forwarding: {'on' if enabled else 'off'} · {chat['name']}. Only the owner's ordinary messages in this Discord channel are forwarded. Configure --notify separately for the return direction.")]
        if action in {'approve', 'cancel_work'}:
            if self.supervisor.agent is None:
                raise ValueError('The conversation tool runtime is not available')
            if action == 'approve':
                self.supervisor.agent.approve((account_id, chat_id), command.task_id)
                self.supervisor.resume(account_id, chat_id)
                return [BotResponse(text='Approved the exact retained actions. The conversation worker will continue.')]
            await self.supervisor.manual_sent(account_id, chat_id)
            return [BotResponse(text='Cancelled this conversation’s pending work.')]
        if action == "messages":
            messages = await self.transport.read_messages(chat_id, command.limit)
            return [BotResponse(text=f"WeChat · {chat['name']}\n" + ("\n".join(
                f"{'Me' if m['outgoing'] else (m.get('sender_name') or ('Participant' if m['kind'] == 'text' else 'Unclassified activity'))}: {m.get('text', '')}" for m in messages) or "No text messages currently exposed."))]
        if action == "send":
            await self.supervisor.manual_sent(account_id, chat_id)
            receipt = await self.transport.send(chat_id, command.message)
            if not receipt.get("verified"):
                return [BotResponse(text="The send could not be verified. Check WeChat before trying again; it was not retried.",
                                    metadata={"tool_status": "failed", "error_code": "send_unverified", "retryable": False})]
            self.store.record(account_id, chat_id, {"id": receipt.get("message_id"), "chat_id": chat_id,
                "sender_id": account_id, "sender_name": self.account["name"], "text": command.message,
                "outgoing": True, "kind": "text"}, source="manual")
            return [BotResponse(text=f"WeChat shows your outgoing message in {chat['name']}.")]
        if action in {"notify", "agent"}:
            changes = {}
            if action == "notify" and command.mode != "status":
                destination='dm:'+owner if command.destination=='dm' else str(message.channel_id)
                changes = {"notify_mode": "on" if command.mode == "on" else "off",
                           "notify_channel": destination if command.mode == "on" else None}
                if command.mode=='on': self._control(destination)
            if action == "agent":
                if command.mode=='on' and not self.store.get(account_id,chat_id)['whitelisted']:
                    raise ValueError('Add this conversation to --whitelist first. Whitelist membership is required for every automatic reply.')
                if command.mode != "status":
                    changes["agent_enabled"] = command.mode == "on"
                for key in ("instructions", "delay_seconds", "max_reply_chars", 'chat_kind', 'mention_aliases'):
                    if getattr(command, key) is not None:
                        changes[key] = getattr(command, key)
            if changes:
                settings = await self.supervisor.configure(account_id, chat_id, **changes)
                messages = await self.transport.read_messages(chat_id, 40)
                await self.supervisor.baseline(account_id, chat_id, messages)
                self._baselined.add((account_id, chat_id))
                self._control(message.channel_id)
                await self.initialize()
            else:
                settings = self.store.get(account_id, chat_id)
            destination = self._destination(account_id, chat_id)
            pending = self.supervisor.agent.pending((account_id, chat_id)) if self.supervisor.agent else None
            files = self.supervisor.agent.file_inventory((account_id, chat_id)) if self.supervisor.agent else []
            routing = next((entry for entry in self.supervisor.status()['routing']
                            if entry['account_id'] == account_id and entry['chat_id'] == chat_id), None)
            return [BotResponse(text=f"WeChat · {chat['name']}\nNotifications: {'on' if destination else 'off'}\n"
                f"Whitelisted: {'yes' if settings['whitelisted'] else 'no'}; kind: {settings['chat_kind']}\n"
                + ('Last incoming routing: ' + json.dumps(routing, ensure_ascii=False) + '\n' if routing else '')
                + f"Reply gate: {'ready' if self.store.reply_ready(account_id,chat_id) else 'paused'}; groups require @mention\n"
                f"Mention names: {', '.join(self.store.mention_names(account_id,chat_id)) or '(not configured)'}\n"
                f"Reply agent: {'on' if settings['agent_enabled'] else 'off'}; engine: {getattr(self.supervisor.generator, 'engine_label', 'custom')}\n"
                f"Minimum send interval: {settings['delay_seconds']:g}s; coalescing: {self.supervisor.coalesce_seconds:g}s; maximum length: {settings['max_reply_chars']}\n"
                f"Instructions: {settings['instructions'] or '(default DeepSeek persona)'}"
                + (f"\nWork: {pending['status']}; task: {pending['task_id']}"
                   + ("\nPending actions: " + json.dumps(pending['actions'], ensure_ascii=False) if pending['actions'] else '')
                   if pending else '')
                + ('\nRetained files: ' + json.dumps(files, ensure_ascii=False) if files else ''))]
        raise ValueError(f"Unsupported WeChat action: {action}")

    async def _poll_once(self):
        cycle_errors = []
        account = await self.transport.status()
        if account.get("state") == "confirming" and asyncio.get_running_loop().time() < self._login_until:
            account = await self.transport.confirm_login()
        previous = self.account.get("state")
        self.account = account
        if account.get("state") != "ready":
            if self._known_account is not None:
                await self.supervisor.reset_baseline()
                self._known_account = None
            if previous == "ready":
                for channel in self._control_channels():
                    await self._send_notice(channel, "WeChat is no longer ready. Notifications and automatic replies are paused.")
            return
        account_id = account["account_id"]
        if await self._sync_account(account_id):
            for channel in self._control_channels():
                await self._send_notice(channel, f"WeChat login confirmed: {account.get('name', account_id)}.")
        chats = await self.transport.list_chats()
        self._chat_names.update({c["id"]: c["name"] for c in chats})
        settings = self.store.list_settings(account_id)
        watched = {s["chat_id"] for s in settings if s["chat_id"] != "*" and
                   ((s["agent_enabled"] and s['whitelisted']) or s.get("notify_mode") == "on")}
        all_enabled = bool(self.store.get(account_id, "*").get("notify_channel"))
        native_reads = getattr(self.transport, 'reads_mark_seen', True) is False
        if all_enabled:
            signatures = {c["id"]: (c.get("unread", 0), c.get("preview", ""), c.get('last_timestamp'))
                          if native_reads else (c.get("unread", 0), c.get("preview", "")) for c in chats}
            if not self._initial_all:
                # Existing unread messages are baseline, not new notifications.
                for chat in chats:
                    if native_reads or chat.get("unread", 0):
                        try:
                            messages = await self.transport.read_messages(chat["id"], 40)
                            await self.supervisor.baseline(account_id, chat["id"], messages)
                            self._baselined.add((account_id, chat["id"]))
                        except RuntimeError as error:
                            cycle_errors.append(str(error))
                self._initial_all = True
            else:
                watched.update(c["id"] for c in chats if (native_reads or c.get("unread", 0))
                               and signatures[c["id"]] != self._all_previews.get(c["id"]))
            self._all_previews = signatures
        for chat_id in watched:
            try:
                messages = await self.transport.read_messages(chat_id, 40)
            except RuntimeError as error:
                cycle_errors.append(str(error))
                if str(error) != self.last_error:
                    print(f'[wechat bridge] read failed for {chat_id}: {error}')
                chat = next((c for c in chats if c['id'] == chat_id), None)
                if chat and chat.get('unread') and self._destination(account_id, chat_id):
                    await self._notify({'account_id': account_id, 'chat_id': chat_id, 'type': 'unread_preview',
                                        'message': chat.get('preview') or 'Unread activity; this view does not expose message contents.'})
                    self._all_previews[chat_id] = (chat.get('unread', 0), chat.get('preview', ''))
                continue
            if all_enabled and (account_id, chat_id) not in self._baselined:
                unread = next((c.get("unread", 0) for c in chats if c["id"] == chat_id), 0)
                old_count = max(0, len(messages) - (max(1,unread) if native_reads else unread))
                await self.supervisor.baseline(account_id, chat_id, messages[:old_count])
                self._baselined.add((account_id, chat_id))
            prior = {item['id'] for item in self.store.history(account_id, chat_id, 50)}
            already_baselined = (account_id, chat_id) in self._baselined
            await self.supervisor.observe(account_id, chat_id, messages)
            self._baselined.add((account_id, chat_id))
            if not already_baselined:
                print(f'[wechat bridge] baseline ready for {chat_id}: {len(messages)} visible messages')
            if already_baselined:
                for item in messages:
                    if item['id'] not in prior and item.get('kind') != 'text' and item.get('outgoing') is not True:
                        await self._notify({'account_id': account_id, 'chat_id': chat_id, 'type': 'activity_preview', 'message': item})
            if all_enabled and chat_id in self._all_previews and not native_reads:
                # Opening the conversation consumes its unread badge. A second
                # identical message must still be noticed at the next poll.
                self._all_previews[chat_id] = (0, self._all_previews[chat_id][1])
        self.last_error = cycle_errors[-1] if cycle_errors else ""

    async def _poll(self):
        native_interval = getattr(self.transport, 'poll_seconds', None)
        interval = native_interval if type(native_interval) in (int,float) else max(3, min(60, int(self.config.get("WECHAT_BRIDGE_POLL_SECONDS", 5))))
        while self._active:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = f"Desktop polling stopped for this cycle ({type(error).__name__})."
                print(f"[wechat bridge] poll failed: {type(error).__name__}")
            await asyncio.sleep(interval)
