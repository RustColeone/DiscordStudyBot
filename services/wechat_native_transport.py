"""Native database reads with the existing serialized, checked desktop writer."""
import asyncio
from collections import defaultdict
from pathlib import Path
import time

from services.wechat_desktop import WeChatDesktopTransport
from services.wechat_native import NativeSchemaError, WeChatNativeReader
from services.request_timing import record


class WeChatNativeTransport(WeChatDesktopTransport):
    read_backend = 'native database'
    reads_mark_seen = False
    poll_seconds = 1

    def __init__(self, config=None, reader=None):
        super().__init__(config)
        self.reader = reader or WeChatNativeReader(self.config['WECHAT_BRIDGE_DATABASE_ROOT'],
            self.config.get('WECHAT_BRIDGE_DATABASE_KEYS') or Path(__file__).resolve().parents[1]/'temp/wechat_native/keys.json')
        self._account_cache = None
        self._account_checked = -1e9
        self._chat_locks = defaultdict(asyncio.Lock)
        self._receipt_before = {}
        self._quote_targets = {}
        self._bindings_loaded = False

    async def bind_settings(self, store, account_id):
        if account_id != self.reader.account_id:
            raise NativeSchemaError('The native database belongs to a different logged-in account')
        store.native_mentions = True
        if self._bindings_loaded:
            return
        for chat_id, username in store.native_bindings(account_id).items():
            self.reader.bind(chat_id, username)
        # Preserve all existing settings/routes/history IDs. A one-time unique
        # name lookup pins that legacy setting to an immutable native username.
        for settings in store.list_settings(account_id):
            target = settings['chat_id']
            if target.startswith('name:') and target not in self.reader.bindings:
                try:
                    chat = self.reader.resolve(target)
                except NativeSchemaError:
                    continue  # remains unavailable; never reassign a whitelist
                store.pin_native_chat(account_id, target, chat['native_id'])
                self.reader.bind(target, chat['native_id'])
        self._bindings_loaded = True

    async def status(self):
        if self._account_cache is None or time.monotonic()-self._account_checked >= 15:
            result = await super().status()
            if result.get('state') == 'ready' and result.get('account_id') != self.reader.account_id:
                result = {'state':'identity_unknown','account_id':'','name':'',
                          'detail':'The native reader and logged-in desktop account differ; replies are paused.'}
            self._account_cache = result
            self._account_checked = time.monotonic()
        return {**self._account_cache, 'read_backend':self.read_backend}

    async def list_chats(self):
        return self.reader.list_chats()

    async def _run_helper(self, operation, **arguments):
        if operation == 'send_file':
            arguments['native_receipt'] = True
        return await super()._run_helper(operation, **arguments)

    async def resolve(self, target):
        if not target or target == 'all':
            raise ValueError('Choose one native recipient with --chats')
        return self.reader.resolve(target)

    async def read_messages(self, chat_id, limit=20):
        chat = self.reader.resolve(chat_id)
        async with self._chat_locks[chat['native_id']]:
            started = time.monotonic()
            items = self.reader.read_messages(chat['id'], limit)
            record('wechat_native_read', duration_s=round(time.monotonic()-started,4), messages=len(items))
            return items

    def _desktop_target(self, target):
        chat = self.reader.resolve(target)
        # UI selection still uses names. Refuse a duplicate visible label even
        # when the database itself could distinguish both native usernames.
        if sum(other['name']==chat['name'] for other in self.reader.list_chats()) != 1:
            raise NativeSchemaError('The desktop recipient label is ambiguous; no send attempted')
        return 'name:'+chat['name']

    async def _prepared_messages(self, target, snapshot):
        account = snapshot['account']
        if account.get('state') != 'ready' or account.get('account_id') != self.reader.account_id:
            return []
        chat = self.reader.resolve(target)
        items = self.reader.read_messages(chat['id'],100,fresh=True)
        target_message = self._quote_targets.get(chat['native_id'])
        if target_message:
            matched = [item for item in items if item['id']==target_message['id']]
            if (len(matched)!=1 or any(matched[0].get(field)!=target_message.get(field)
                    for field in ('text','sender_id','server_id'))):
                raise NativeSchemaError('The quoted trigger identity changed or is unavailable; no send attempted')
            target_message['ui_context'] = [{field:item.get(field) for field in
                ('id','text','kind','reply_to_server_id')} for item in items]
        self._receipt_before[chat['native_id']] = {item['id'] for item in items}
        return items

    def _delivery_options(self):
        return {'native_receipt':True}

    async def _delivery_receipt(self, target, receipt, text):
        if not receipt.get('verified') and not receipt.get('sent'):
            return receipt
        chat = self.reader.resolve(target)
        if chat['native_id'] not in self._receipt_before:
            return {'verified':False,'reason':'native_receipt_baseline_missing'}
        before = self._receipt_before[chat['native_id']]
        # Never retry Send. Wait only for its committed native outgoing row.
        deadline = time.monotonic()+(600 if text is None else 3)
        while True:
            latest = self.reader.read_messages(chat['id'],100,fresh=True)
            candidates = [item for item in latest if item['id'] not in before and item['outgoing'] is True
                          and (item['text']==text if text is not None else item['kind']!='text')]
            if text is None:
                # A locally staged upload has server_id=0. Require the server's
                # committed ID before acknowledging delivery.
                candidates = [item for item in candidates if str(item.get('server_id') or '0').isdigit()
                              and int(item.get('server_id') or 0) > 0]
            quoted = self._quote_targets.get(chat['native_id'])
            if quoted:
                candidates = [item for item in candidates if item.get('reply_to_server_id')==quoted['server_id']]
            if len(candidates) == 1:
                return {**receipt,'verified':True,'message_id':candidates[0]['id'],'read_backend':self.read_backend}
            if len(candidates)>1 or time.monotonic() >= deadline:
                return {'verified':False,'reason':'native_outgoing_receipt_ambiguous','desktop_verified':bool(receipt.get('verified'))}
            await asyncio.sleep(.1)

    async def send_checked(self, chat_id, text, expected_account, prepare, authorize, on_commit, reply_to=None):
        chat = self.reader.resolve(chat_id)
        async with self._chat_locks[chat['native_id']]:
            reply_to = dict(reply_to) if reply_to else None
            if reply_to and (not str(reply_to.get('server_id','')).isdigit() or reply_to['server_id']=='0'):
                raise NativeSchemaError('The quote target has no committed native server ID')
            self._quote_targets[chat['native_id']] = reply_to
            # The helper holds UI selection through prepare/commit. Its fresh
            # account check remains authoritative; native reads do not use the UI lock.
            return await super().send_checked(self._desktop_target(chat_id),text,expected_account,
                                             prepare,authorize,on_commit,reply_to=reply_to)

    async def send(self, chat_id, text, authorize=None, expected_account=None):
        chat = self.reader.resolve(chat_id)
        async with self._chat_locks[chat['native_id']]:
            self._quote_targets[chat['native_id']] = None
            before = self.reader.read_messages(chat['id'],100,fresh=True)
            self._receipt_before[chat['native_id']] = {item['id'] for item in before}
            receipt = await super().send(self._desktop_target(chat_id),text,authorize,expected_account or self.reader.account_id)
            return await self._delivery_receipt(chat['id'],receipt,text)

    async def send_file(self, chat_id, path, filename=None, authorize=None, expected_account=None):
        chat = self.reader.resolve(chat_id)
        async with self._chat_locks[chat['native_id']]:
            self._quote_targets[chat['native_id']] = None
            before = self.reader.read_messages(chat['id'],100,fresh=True)
            self._receipt_before[chat['native_id']] = {item['id'] for item in before}
            receipt = await super().send_file(self._desktop_target(chat_id),path,filename,authorize,expected_account or self.reader.account_id)
            return await self._delivery_receipt(chat['id'],receipt,None)

    async def close(self):
        self.reader.close()
        await super().close()
