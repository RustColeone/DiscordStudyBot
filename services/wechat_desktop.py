"""Serialized account transport for the native Linux desktop client.

The helper uses system Python for GI/AT-SPI; the bot's venv stays independent of
desktop packages. No HTTP listener, account cookies, or database decryption.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import uuid
from difflib import SequenceMatcher
import re
import time

from services.request_timing import record


class WeChatDesktopTransport:
    policy_guarded_sends = True
    atomic_checked_sends = True
    supports_quoted_replies = True
    def __init__(self, config=None):
        self.config = config or {}
        self._lock = asyncio.Lock()
        self.helper = Path(__file__).resolve().parents[1] / "scripts" / "wechat_desktop.py"
        self.last_error = ""
        self._views = {}

    def _environment(self):
        env = dict(os.environ)
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        runtime = str(self.config.get("WECHAT_BRIDGE_RUNTIME_DIR") or f"/run/user/{uid}")
        env.update(DISPLAY=str(self.config.get("WECHAT_BRIDGE_DISPLAY") or ":0"),
                   WAYLAND_DISPLAY=str(self.config.get("WECHAT_BRIDGE_WAYLAND_DISPLAY") or "wayland-0"),
                   XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus",
                   XAUTHORITY=str(Path.home() / ".Xauthority"), QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1", QT_QPA_PLATFORM="xcb")
        return env

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
            record('wechat_desktop_operation', operation=operation,
                   queue_wait_s=round((acquired_at or finished)-queued_at, 3),
                   duration_s=round(finished-acquired_at, 3) if acquired_at is not None else 0,
                   error_code=error_code)

    async def _run_helper(self, operation, **arguments):
        if operation == 'checked_send':
            return await self._checked_helper(**arguments)
        process = await asyncio.create_subprocess_exec(
            str(self.config.get("WECHAT_BRIDGE_PYTHON") or "/usr/bin/python3"), str(self.helper),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self._environment())
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(
                {"operation": operation, **arguments}, ensure_ascii=False).encode()), timeout=120 if operation == 'send_file' else 25)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError("WeChat desktop operation did not finish; a send will not be retried automatically.")
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("WeChat desktop helper is unavailable. Install at-spi2-core and gir1.2-atspi-2.0.") from None
        if not result.get("ok"):
            self.last_error = result.get("error", "WeChat desktop operation failed")
            raise RuntimeError(self.last_error)
        self.last_error = ""
        return result["result"]

    async def _checked_helper(self, target, text, expected_account, prepare, authorize, on_commit, reply_to=None):
        process = await asyncio.create_subprocess_exec(
            str(self.config.get('WECHAT_BRIDGE_PYTHON') or '/usr/bin/python3'), str(self.helper),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self._environment(), limit=2_000_000)
        errors = asyncio.create_task(process.stderr.read())
        async def write(value):
            process.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode())
            await process.stdin.drain()
        async def read():
            raw = await process.stdout.readline()
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError('The desktop delivery session ended without a result') from None
            if not result.get('ok'):
                raise RuntimeError(result.get('error', 'Desktop delivery failed'))
            return result['result']
        try:
            async with asyncio.timeout(25):
                request = {'operation':'checked_send', 'target':target, 'expected_account':expected_account,
                           **self._delivery_options()}
                if reply_to:
                    request['reply_to'] = reply_to
                await write(request)
                snapshot = await read()
                record('wechat_delivery_prepared', visible_messages=len(snapshot['messages']))
                latest = await self._prepared_messages(target, snapshot)
                decision = await prepare(snapshot['account'], latest)
                if decision is not None:
                    return decision
                authorize()
                on_commit()
                record('wechat_delivery_commit')
                await write({'commit':True, 'text':text, **({'reply_to':reply_to} if reply_to else {})})
                receipt = await read()
                await process.wait()
                return await self._delivery_receipt(target, receipt, text)
        except asyncio.TimeoutError:
            raise RuntimeError('Desktop delivery timed out; it will not be retried automatically') from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            await errors

    async def _prepared_messages(self, target, snapshot):
        return self._normalize_view(target, snapshot['messages'])

    def _delivery_options(self):
        return {}

    async def _delivery_receipt(self, target, receipt, text):
        return receipt

    async def status(self):
        try:
            return await self._call("status")
        except (RuntimeError, OSError) as error:
            return {"state": "unavailable", "account_id": "", "name": "", "detail": str(error)}

    async def login(self):
        directory = Path(__file__).resolve().parents[1] / "temp" / "wechat_login"
        directory.mkdir(parents=True, exist_ok=True)
        return await self._call("login", output_dir=str(directory))

    async def list_chats(self):
        return await self._call("chats")

    async def confirm_login(self):
        return await self._call("confirm_login")

    async def resolve(self, target):
        if not target or target == "all":
            raise ValueError("Choose a specific recipient with --to or --chat; use --chats to see IDs.")
        return await self._call("resolve", target=target)

    async def read_messages(self, chat_id, limit=20):
        items = await self._call("messages", target=chat_id, limit=limit)
        return self._normalize_view(chat_id, items)

    def _normalize_view(self, chat_id, items):
        previous = self._views.get(chat_id, [])
        def key(item):
            return item.get('content_key') or (item.get('sender_id'), item.get('text'))
        matcher = SequenceMatcher(a=[key(m) for m in previous], b=[key(m) for m in items], autojunk=False)
        for old_start, new_start, count in matcher.get_matching_blocks():
            for offset in range(count):
                old, new = previous[old_start + offset], items[new_start + offset]
                old_time, new_time = old.get('timestamp'), new.get('timestamp')
                if old_time and new_time and old_time != new_time:
                    continue
                # Qt virtualizes old time rows as new media pushes history up.
                # Preserve overlapping row identity when its time anchor disappears.
                if str(new.get('id', '')).startswith('ui:'):
                    new['id'] = old['id']
                    if not new_time:
                        new['timestamp'] = old_time or ''
        self._views[chat_id] = [dict(m) for m in items]
        return items

    async def send(self, chat_id, text, authorize=None, expected_account=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("A WeChat message must contain 1 to 4000 characters")
        return await self._call("send", target=chat_id, text=text, _authorize=authorize, expected_account=expected_account)

    async def send_checked(self, chat_id, text, expected_account, prepare, authorize, on_commit, reply_to=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('A WeChat message must contain 1 to 4000 characters')
        options = {'reply_to':reply_to} if reply_to else {}
        return await self._call('checked_send', _authorize=authorize, target=chat_id, text=text,
            expected_account=expected_account, prepare=prepare, authorize=authorize, on_commit=on_commit, **options)

    async def send_file(self, chat_id, path, filename=None, authorize=None, expected_account=None):
        source = Path(path).resolve()
        limit = max(1, min(100, float(self.config.get('WECHAT_BRIDGE_MAX_UPLOAD_MB', 100))))
        if not source.is_file() or not 0 < source.stat().st_size <= limit * 1024 * 1024:
            raise ValueError('The prepared file is missing, empty, or exceeds the configured attachment budget')
        name = filename or source.name
        if Path(name).name != name or not name.strip() or '\n' in name or '\r' in name:
            raise ValueError('Attachment names must be a simple filename')
        root = self.helper.parents[1] / 'temp' / 'wechat_outbox'
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
        return await self._call('send_file', target=chat_id, path=str(staged), _authorize=authorize, expected_account=expected_account)

    async def close(self):
        # Disconnect the integration, not the account's logged-in desktop session.
        pass
