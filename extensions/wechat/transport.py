"""Native database reads with the serialized, checked desktop writer.

Merged from services/wechat_desktop.py (desktop helper calls) and services/wechat_native_transport.py
(native reads and receipts). Reading uses only the account's databases; sending, login and account
status use the desktop helper (`desktop_helper.py`, run with the system Python for GI/AT-SPI). An
uncertain send is never retried.
"""
import asyncio
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .database_pages import DatabaseSnapshotError
from .native import NativeSchemaError, WeChatNativeReader

HELPER = Path(__file__).resolve().parent / 'desktop_helper.py'


SAFE_ERROR = re.compile(r"no (?:send|message|file|click)(?: was)? (?:attempted|sent|chosen)|nothing was sent", re.I)


class SendFailed(RuntimeError):
    """Nothing was sent."""


class SendUncertain(RuntimeError):
    """The message may have been sent. Never retry it."""


def classify(error, committed=True):
    """Desktop errors before Send, or ones the helper marks as safe, mean nothing was sent."""
    if isinstance(error, (SendFailed, SendUncertain)):
        return error
    if not committed or SAFE_ERROR.search(str(error)):
        return SendFailed(str(error) or type(error).__name__)
    return SendUncertain(str(error) or type(error).__name__)


class WeChatTransport:
    read_backend = 'native database'

    def __init__(self, settings, reader=None, log=None):
        self.settings = dict(settings)
        if reader is None and not self.settings.get('database_root'):
            raise ValueError('Set extension_config.wechat.database_root (or WECHAT_BRIDGE_DATABASE_ROOT) to the '
                             "account's db_storage directory")
        self.reader = reader or WeChatNativeReader(self.settings['database_root'], self.settings['database_keys'])
        # Snapshots (file copies, hashing, page decryption) are slow; they must not block the event loop.
        # One worker: the reader's connection and contact caches are not thread-safe.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wechat-reader')
        self.log = log or (lambda event, **fields: None)
        self.helper = HELPER
        self.outbox = Path(self.settings['outbox_dir'])
        self.login_dir = Path(self.settings['login_dir'])
        self.last_error = ''
        self._lock = asyncio.Lock()
        self._account_cache = None
        self._account_checked = -1e9
        self._chat_locks = defaultdict(asyncio.Lock)
        # Receipts verified by this transport, so observation never mistakes them for manual activity.
        self.own_message_ids = deque(maxlen=200)
        self._receipt_before = {}
        self._quote_targets = {}

    @property
    def account_id(self):
        return self.reader.account_id

    @property
    def username(self):
        return self.reader.username

    # ---- desktop helper ---------------------------------------------------------------------------
    def _environment(self):
        env = dict(os.environ)
        uid = os.getuid() if hasattr(os, 'getuid') else 1000
        runtime = str(self.settings.get('runtime_dir') or f'/run/user/{uid}')
        env.update(DISPLAY=str(self.settings.get('display') or ':0'),
                   WAYLAND_DISPLAY=str(self.settings.get('wayland_display') or 'wayland-0'),
                   XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f'unix:path={runtime}/bus',
                   XAUTHORITY=str(Path.home() / '.Xauthority'), QT_LINUX_ACCESSIBILITY_ALWAYS_ON='1',
                   QT_QPA_PLATFORM='xcb', WECHAT_OUTBOX=str(self.outbox))
        return env

    def _python(self):
        return str(self.settings.get('python') or '/usr/bin/python3')

    async def _call(self, operation, _authorize=None, **arguments):
        queued_at = time.monotonic()
        acquired_at = None
        error_code = None
        try:
            async with self._lock:
                acquired_at = time.monotonic()
                if _authorize is not None:
                    _authorize()
                return await self._run_helper(operation, **arguments)
        except BaseException as error:
            error_code = type(error).__name__
            raise
        finally:
            finished = time.monotonic()
            self.log('desktop_operation', operation=operation,
                     queue_wait_s=round((acquired_at or finished)-queued_at, 3),
                     duration_s=round(finished-acquired_at, 3) if acquired_at is not None else 0,
                     error_code=error_code)

    async def _run_helper(self, operation, **arguments):
        if operation == 'checked_send':
            return await self._checked_helper(**arguments)
        if operation == 'send_file':
            arguments['native_receipt'] = True
        process = await asyncio.create_subprocess_exec(
            self._python(), str(self.helper), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._environment())
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(
                {'operation': operation, **arguments}, ensure_ascii=False).encode()),
                timeout=120 if operation == 'send_file' else 25)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise TimeoutError('WeChat desktop operation did not finish; a send will not be retried automatically.')
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('WeChat desktop helper is unavailable. Install at-spi2-core and gir1.2-atspi-2.0.') from None
        if not result.get('ok'):
            self.last_error = result.get('error', 'WeChat desktop operation failed')
            raise RuntimeError(self.last_error)
        self.last_error = ''
        return result['result']

    async def _checked_helper(self, target, text, expected_account, prepare, authorize, on_commit, reply_to=None):
        process = await asyncio.create_subprocess_exec(
            self._python(), str(self.helper), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._environment(), limit=2_000_000)
        errors = asyncio.create_task(process.stderr.read())

        async def write(value):
            process.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode())
            await process.stdin.drain()

        async def read():
            raw = await process.stdout.readline()
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise TimeoutError('The desktop delivery session ended without a result') from None
            if not result.get('ok'):
                raise RuntimeError(result.get('error', 'Desktop delivery failed'))
            return result['result']
        committed = False
        try:
            async with asyncio.timeout(25):
                request = {'operation': 'checked_send', 'target': target, 'expected_account': expected_account,
                           'native_receipt': True}
                if reply_to:
                    request['reply_to'] = reply_to
                await write(request)
                snapshot = await read()
                latest = await self._prepared_messages(target, snapshot)
                decision = await prepare(snapshot['account'], latest)
                if decision is not None:
                    return decision
                authorize()
                on_commit()
                committed = True
                self.log('delivery_commit')
                await write({'commit': True, 'text': text, **({'reply_to': reply_to} if reply_to else {})})
                receipt = await read()
                await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, TimeoutError) and committed:
                raise SendUncertain('Desktop delivery timed out; it will not be retried automatically') from None
            raise classify(error, committed) from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            await errors
        return await self._delivery_receipt(target, receipt, text)

    # ---- account ----------------------------------------------------------------------------------
    async def status(self, refresh=False):
        if refresh or self._account_cache is None or time.monotonic()-self._account_checked >= float(
                self.settings.get('status_seconds', 15)):
            try:
                result = await self._call('status')
            except (RuntimeError, OSError) as error:
                result = {'state': 'unavailable', 'account_id': '', 'name': '', 'detail': str(error)}
            if result.get('state') == 'ready' and result.get('account_id') != self.reader.account_id:
                result = {'state': 'identity_unknown', 'account_id': '', 'name': '',
                          'detail': 'The native reader and logged-in desktop account differ; replies are paused.'}
            self._account_cache = result
            self._account_checked = time.monotonic()
        return {**self._account_cache, 'read_backend': self.read_backend}

    async def login(self):
        self.login_dir.mkdir(parents=True, exist_ok=True)
        result = await self._call('login', output_dir=str(self.login_dir))
        self._account_cache = None
        return result

    async def confirm_login(self):
        result = await self._call('confirm_login')
        self._account_cache = None
        return result

    # ---- reading ----------------------------------------------------------------------------------
    async def _read(self, function, *arguments, **options):
        """Run one reader call on the reader's own thread."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, functools.partial(function, *arguments, **options))

    async def list_chats(self):
        return await self._read(self.reader.list_chats)

    async def resolve(self, target):
        if not target or target == 'all':
            raise ValueError('Choose one WeChat conversation by name or ID')
        return await self._read(self.reader.resolve, target)

    async def read_messages(self, chat_id, limit=20):
        chat = await self._read(self.reader.resolve, chat_id)
        async with self._chat_locks[chat['native_id']]:
            started = time.monotonic()
            items = await self._read(self.reader.read_chat, chat, limit)
            self.log('native_read', duration_s=round(time.monotonic()-started, 4), messages=len(items))
            return items

    def chat_busy(self, chat_id):
        """True while this chat's send/receipt correlation holds its lock; polling should skip it, not wait.

        `chat_id` is the chat's `native:<username>` ID, so this needs no database read.
        """
        return self._chat_locks[chat_id.removeprefix('native:')].locked()

    # ---- sending ----------------------------------------------------------------------------------
    def _chat_and_target(self, target):
        """Runs on the reader thread: the chat and its unambiguous desktop label."""
        chat = self.reader.resolve(target)
        # UI selection still uses names. Refuse a duplicate visible label even
        # when the database itself could distinguish both native usernames.
        if sum(other['name'] == chat['name'] for other in self.reader.list_chats()) != 1:
            raise NativeSchemaError('The desktop recipient label is ambiguous; no send attempted')
        return chat, 'name:'+chat['name']

    async def _prepared_messages(self, target, snapshot):
        account = snapshot['account']
        if account.get('state') != 'ready' or account.get('account_id') != self.reader.account_id:
            return []
        chat = await self._read(self.reader.resolve, target)
        items = await self._read(self.reader.read_chat, chat, 100, fresh=True)
        target_message = self._quote_targets.get(chat['native_id'])
        if target_message:
            matched = [item for item in items if item['id'] == target_message['id']]
            if (len(matched) != 1 or any(matched[0].get(field) != target_message.get(field)
                                         for field in ('text', 'sender_id', 'server_id'))):
                raise NativeSchemaError('The quoted trigger identity changed or is unavailable; no send attempted')
            target_message['ui_context'] = [{field: item.get(field) for field in
                                             ('id', 'text', 'kind', 'reply_to_server_id')} for item in items]
        self._receipt_before[chat['native_id']] = {item['id'] for item in items}
        return items

    async def _delivery_receipt(self, target, receipt, text):
        if not receipt.get('verified') and not receipt.get('sent'):
            return receipt
        chat = await self._read(self.reader.resolve, target)
        if chat['native_id'] not in self._receipt_before:
            return {'verified': False, 'reason': 'native_receipt_baseline_missing'}
        before = self._receipt_before[chat['native_id']]
        # Never retry Send. Wait only for its committed native outgoing row.
        deadline = time.monotonic()+(600 if text is None else 3)
        interval = 1 if text is None else .1
        transient_errors = 0
        while True:
            try:
                latest = await self._read(self.reader.read_chat, chat, 100, fresh=True)
            except DatabaseSnapshotError:
                # WeChat rewrites its WAL continuously while uploading; one busy
                # snapshot must not abandon an upload that is still progressing.
                transient_errors += 1
                if time.monotonic() >= deadline:
                    return {'verified': False, 'reason': 'native_receipt_unreadable'}
                await asyncio.sleep(interval)
                continue
            candidates = [item for item in latest if item['id'] not in before and item['outgoing'] is True
                          and (item['text'] == text if text is not None else item['kind'] != 'text')]
            if text is None:
                # A locally staged upload has server_id=0. Require the server's
                # committed ID before acknowledging delivery.
                candidates = [item for item in candidates if str(item.get('server_id') or '0').isdigit()
                              and int(item.get('server_id') or 0) > 0]
            quoted = self._quote_targets.get(chat['native_id'])
            if quoted:
                candidates = [item for item in candidates if item.get('reply_to_server_id') == quoted['server_id']]
            if len(candidates) == 1:
                if transient_errors:
                    self.log('receipt_transient_reads', count=transient_errors)
                self.own_message_ids.append(candidates[0]['id'])
                return {**receipt, 'verified': True, 'message_id': candidates[0]['id']}
            if len(candidates) > 1 or time.monotonic() >= deadline:
                return {'verified': False, 'reason': 'native_outgoing_receipt_ambiguous'}
            await asyncio.sleep(interval)

    async def send_checked(self, chat_id, text, prepare, authorize, on_commit, reply_to=None):
        """Select the chat, let `prepare(account, latest)` re-check (None = go on), then Send once.

        Returns the receipt: `verified` with `message_id`, `cancelled`, or unverified with a reason.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('A WeChat message must contain 1 to 4000 characters')
        try:
            chat, target = await self._read(self._chat_and_target, chat_id)
        except DatabaseSnapshotError as error:
            raise SendFailed(str(error)) from None
        async with self._chat_locks[chat['native_id']]:
            reply_to = dict(reply_to) if reply_to else None
            if reply_to and (not str(reply_to.get('server_id', '')).isdigit() or reply_to['server_id'] == '0'):
                raise SendFailed('The quote target has no committed native server ID; no send attempted')
            self._quote_targets[chat['native_id']] = reply_to
            # The helper holds UI selection through prepare/commit. Its fresh
            # account check remains authoritative; native reads do not use the UI lock.
            options = {'reply_to': reply_to} if reply_to else {}
            return await self._call('checked_send', _authorize=authorize, target=target, text=text,
                                    expected_account=self.reader.account_id, prepare=prepare,
                                    authorize=authorize, on_commit=on_commit, **options)

    async def send_file(self, chat_id, path, filename=None, authorize=None):
        try:
            chat, target = await self._read(self._chat_and_target, chat_id)
        except DatabaseSnapshotError as error:
            raise SendFailed(str(error)) from None
        async with self._chat_locks[chat['native_id']]:
            self._quote_targets[chat['native_id']] = None
            try:
                before = await self._read(self.reader.read_chat, chat, 100, fresh=True)
                staged = await asyncio.to_thread(self._stage, path, filename)
            except (OSError, ValueError, DatabaseSnapshotError) as error:
                raise SendFailed(str(error)) from None
            self._receipt_before[chat['native_id']] = {item['id'] for item in before}
            try:
                receipt = await self._call('send_file', target=target, path=str(staged),
                                           _authorize=authorize, expected_account=self.reader.account_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise classify(error) from None
            return await self._delivery_receipt(chat['id'], receipt, None)

    def _stage(self, path, filename=None):
        source = Path(path).resolve()
        limit = max(1.0, min(100.0, float(self.settings.get('max_upload_mb', 100))))
        if not source.is_file() or not 0 < source.stat().st_size <= limit * 1024 * 1024:
            raise ValueError('The prepared file is missing, empty, or exceeds the configured attachment budget')
        name = filename or source.name
        if Path(name).name != name or not name.strip() or '\n' in name or '\r' in name:
            raise ValueError('Attachment names must be a simple filename')
        root = self.outbox
        if root.exists():
            for old in root.iterdir():
                if (old.is_dir() and not old.is_symlink() and re.fullmatch(r'[0-9a-f]{32}', old.name)
                        and old.stat().st_mtime < time.time() - 86400):
                    shutil.rmtree(old)
        directory = root / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        staged = directory / name
        # Keep a private copy while the desktop uploads asynchronously.
        shutil.copyfile(source, staged)
        staged.chmod(0o600)
        return staged

    async def close(self):
        # Disconnect the integration, not the account's logged-in desktop session.
        try:
            await self._read(self.reader.close)
        finally:
            self._executor.shutdown(wait=False)
