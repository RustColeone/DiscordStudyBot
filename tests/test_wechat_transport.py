import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services.wechat_desktop import WeChatDesktopTransport


class WeChatTransportTests(unittest.IsolatedAsyncioTestCase):
    def delivery_process(self, snapshot):
        writer = SimpleNamespace(write=lambda data: writes.append(json.loads(data)), drain=AsyncMock())
        writes = []
        process = SimpleNamespace(stdin=writer,
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
                json.dumps({'ok':True,'result':snapshot}).encode(),
                b'{"ok":true,"result":{"verified":true,"message_id":"sent"}}'])),
            stderr=SimpleNamespace(read=AsyncMock(return_value=b'')), returncode=None)
        process.kill = lambda: setattr(process, 'returncode', -9)
        async def wait():
            if process.returncode is None: process.returncode = 0
        process.wait = AsyncMock(side_effect=wait)
        return process, writes

    async def test_checked_delivery_requires_explicit_commit_after_latest_message_callback(self):
        process, writes = self.delivery_process({'account':{'state':'ready','account_id':'account'},
                                                 'messages':[{'id':'new','text':'incoming'}]})
        transport = WeChatDesktopTransport()
        async def prepare(account, messages):
            self.assertEqual(len(writes),1)
            self.assertEqual(account['account_id'],'account')
            self.assertEqual(messages[0]['id'],'new')
        commit = AsyncMock()
        committed = []
        with patch('services.wechat_desktop.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            receipt = await transport.send_checked('chat','reply','account',prepare,lambda:None,
                                                   lambda:committed.append(True))
        self.assertTrue(receipt['verified'])
        self.assertEqual(writes[1], {'commit':True,'text':'reply'})
        self.assertEqual(committed,[True])

    async def test_checked_delivery_never_commits_after_cancel_deferral_or_revocation(self):
        for decision in ({'cancelled':True},{'deferred':True},'revoked'):
            process, writes = self.delivery_process({'account':{},'messages':[]})
            allowed = True
            def authorize():
                if not allowed: raise PermissionError('revoked')
            async def prepare(account, messages):
                nonlocal allowed
                if decision == 'revoked':
                    allowed=False
                    return None
                return decision
            committed=[]
            with patch('services.wechat_desktop.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
                if decision == 'revoked':
                    with self.assertRaises(PermissionError):
                        await WeChatDesktopTransport().send_checked('chat','reply','account',prepare,authorize,
                                                                   lambda:committed.append(True))
                else:
                    self.assertEqual(await WeChatDesktopTransport().send_checked('chat','reply','account',prepare,authorize,
                                       lambda:committed.append(True)),decision)
            self.assertEqual(len(writes),1)
            self.assertEqual(committed,[])
            self.assertEqual(process.returncode,-9)

    async def test_operation_timing_distinguishes_queue_execution_and_failure_without_payload(self):
        transport = WeChatDesktopTransport()
        transport._run_helper = AsyncMock(side_effect=RuntimeError('helper unavailable'))
        await transport._lock.acquire()
        with patch('services.wechat_desktop.record') as timing:
            pending = asyncio.create_task(transport.send('private chat', 'private message'))
            await asyncio.sleep(0)
            transport._lock.release()
            with self.assertRaises(RuntimeError):
                await pending
        args, values = timing.call_args
        self.assertEqual(args, ('wechat_desktop_operation',))
        self.assertEqual(values['operation'], 'send')
        self.assertEqual(values['error_code'], 'RuntimeError')
        self.assertGreaterEqual(values['queue_wait_s'], 0)
        self.assertGreaterEqual(values['duration_s'], 0)
        self.assertNotIn('private', str(timing.call_args))

    async def test_revocation_while_waiting_for_desktop_lock_prevents_send(self):
        transport = WeChatDesktopTransport()
        allowed = True
        def authorize():
            if not allowed:
                raise PermissionError('Whitelist revoked')
        await transport._lock.acquire()
        spawn = AsyncMock()
        with patch('services.wechat_desktop.asyncio.create_subprocess_exec', spawn):
            running = asyncio.create_task(transport.send('chat', 'hello', authorize=authorize, expected_account='account'))
            await asyncio.sleep(0)
            allowed = False
            transport._lock.release()
            with self.assertRaises(PermissionError):
                await running
        spawn.assert_not_awaited()

    async def test_virtualized_time_anchor_does_not_make_old_outgoing_message_new(self):
        transport = WeChatDesktopTransport()
        transport._call = AsyncMock(side_effect=[
            [{'id':'ui:old', 'text':'own reply', 'outgoing':True, 'timestamp':'17:05'}],
            [{'id':'ui:drifted', 'text':'own reply', 'outgoing':None, 'timestamp':''},
             {'id':'ui:video', 'text':'Video 2:01', 'outgoing':True, 'timestamp':'17:20'}]])
        first = await transport.read_messages('chat')
        second = await transport.read_messages('chat')
        self.assertEqual(first[0]['id'], second[0]['id'])
        self.assertEqual(second[0]['timestamp'], '17:05')
        self.assertIsNone(second[0]['outgoing'])

    async def test_identical_text_at_a_new_known_time_is_a_new_message(self):
        transport = WeChatDesktopTransport()
        transport._call = AsyncMock(side_effect=[
            [{'id':'ui:old', 'text':'hello', 'timestamp':'17:05'}],
            [{'id':'ui:new', 'text':'hello', 'timestamp':'17:25'}]])
        await transport.read_messages('chat')
        self.assertEqual((await transport.read_messages('chat'))[0]['id'], 'ui:new')

    async def test_helper_cancellation_kills_process_and_stays_cancelled(self):
        started = asyncio.Event()
        async def communicate(data):
            started.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(communicate=communicate, returncode=None,
                                  kill=lambda: setattr(process, 'returncode', -9), wait=AsyncMock())
        with patch('services.wechat_desktop.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            running = asyncio.create_task(WeChatDesktopTransport()._call('messages'))
            await started.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError): await running
        self.assertEqual(process.returncode, -9)
        process.wait.assert_awaited_once()
