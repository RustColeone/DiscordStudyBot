import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services.wechat_native_transport import WeChatNativeTransport
from services.wechat_native import NativeSchemaError


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_receipt_waits_for_server_commit_without_desktop_verification(self):
        self.transport._receipt_before['room@chatroom'] = set()
        media = {'id':'native:video','outgoing':True,'kind':'video','text':'[Video]','server_id':'0'}
        self.reader.read_messages = unittest.mock.Mock(side_effect=[[media],[{**media,'server_id':'123'}]])
        with patch('services.wechat_native_transport.asyncio.sleep', AsyncMock()):
            receipt = await self.transport._delivery_receipt('Group', {'sent':True,'verified':False}, None)
        self.assertTrue(receipt['verified'])
        self.assertEqual(receipt['message_id'], 'native:video')
        self.assertEqual(self.reader.read_messages.call_count, 2)


    async def test_file_helper_uses_native_receipt_mode(self):
        with patch('services.wechat_desktop.WeChatDesktopTransport._run_helper', AsyncMock(return_value={'sent':True})) as helper:
            await self.transport._run_helper('send_file', target='Group', path='/tmp/video.mp4')
        self.assertTrue(helper.await_args.kwargs['native_receipt'])

    def setUp(self):
        self.chat = {'id':'name:Group','native_id':'room@chatroom','name':'Group'}
        self.reader = SimpleNamespace(account_id='account',bindings={},close=lambda:None,
            list_chats=lambda:[self.chat],resolve=lambda target:self.chat,read_messages=lambda *a,**kw:[])
        self.transport = WeChatNativeTransport({},self.reader)

    async def test_reads_bypass_busy_desktop_lock_and_cached_status_is_bounded(self):
        await self.transport._lock.acquire()
        try:
            self.assertEqual(await self.transport.read_messages('name:Group'),[])
            self.assertEqual(await self.transport.list_chats(),[self.chat])
        finally:self.transport._lock.release()
        with patch('services.wechat_desktop.WeChatDesktopTransport.status',AsyncMock(return_value={'state':'ready','account_id':'account'})) as status:
            await self.transport.status();await self.transport.status()
            self.assertEqual(status.await_count,1)
            self.transport._account_checked -= 16
            await self.transport.status()
            self.assertEqual(status.await_count,2)

    async def test_mismatched_account_pauses_reader(self):
        with patch('services.wechat_desktop.WeChatDesktopTransport.status',AsyncMock(return_value={'state':'ready','account_id':'other'})):
            self.assertEqual((await self.transport.status())['state'],'identity_unknown')

    async def test_quote_preflight_and_receipt_bind_to_original_server_id(self):
        target={'id':'native:trigger','text':'@Me question','sender_id':'person','server_id':'123','outgoing':False}
        self.transport._quote_targets['room@chatroom']=target
        self.reader.read_messages=lambda *a,**kw:[target]
        await self.transport._prepared_messages('Group',{'account':{'state':'ready','account_id':'account'}})
        reply={'id':'native:reply','text':'answer','outgoing':True,'reply_to_server_id':'123'}
        self.reader.read_messages=lambda *a,**kw:[target,reply]
        self.assertTrue((await self.transport._delivery_receipt('Group',{'sent':True},'answer'))['verified'])
        reply['reply_to_server_id']='wrong'
        with patch('services.wechat_native_transport.time.monotonic',side_effect=[0,10]):
            self.assertFalse((await self.transport._delivery_receipt('Group',{'sent':True},'answer'))['verified'])
        self.reader.read_messages=lambda *a,**kw:[target,{**target,'id':'native:duplicate','server_id':'456'}]
        await self.transport._prepared_messages('Group',{'account':{'state':'ready','account_id':'account'}})
        self.assertEqual([m['id'] for m in target['ui_context']],['native:trigger','native:duplicate'])
        self.reader.read_messages=lambda *a,**kw:[{**target,'server_id':'changed'}]
        with self.assertRaises(NativeSchemaError):
            await self.transport._prepared_messages('Group',{'account':{'state':'ready','account_id':'account'}})

    async def test_prepare_uses_native_messages_and_receipt_requires_one_new_outgoing_id(self):
        old = {'id':'native:old','outgoing':True,'text':'same'}
        new = {'id':'native:new','outgoing':True,'text':'reply'}
        self.reader.read_messages = lambda *a,**kw:[old]
        latest = await self.transport._prepared_messages('Group',{'account':{'state':'ready','account_id':'account'},'messages':[{'id':'ui:ignored'}]})
        self.assertEqual(latest,[old])
        self.reader.read_messages = lambda *a,**kw:[old,new]
        receipt = await self.transport._delivery_receipt('Group',{'sent':True,'verified':False},'reply')
        self.assertTrue(receipt['verified'])
        self.assertEqual(receipt['message_id'],'native:new')
        self.reader.read_messages = lambda *a,**kw:[old,new,{**new,'id':'native:duplicate'}]
        receipt = await self.transport._delivery_receipt('Group',{'sent':True},'reply')
        self.assertFalse(receipt['verified'])

    async def test_chat_reads_wait_until_delivery_finishes_and_labels_must_be_unique(self):
        await self.transport._chat_locks['room@chatroom'].acquire()
        pending = asyncio.create_task(self.transport.read_messages('Group'))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.transport._chat_locks['room@chatroom'].release()
        self.assertEqual(await pending,[])
        self.reader.list_chats = lambda:[self.chat,{**self.chat,'native_id':'other'}]
        with self.assertRaises(NativeSchemaError):self.transport._desktop_target('Group')

    async def test_full_delivery_commits_after_native_preflight_and_returns_native_receipt(self):
        writes = []
        process = SimpleNamespace(stdin=SimpleNamespace(write=lambda raw:writes.append(json.loads(raw)),drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
                b'{"ok":true,"result":{"account":{"state":"ready","account_id":"account"},"messages":[]}}',
                b'{"ok":true,"result":{"sent":true,"verified":false}}'])),
            stderr=SimpleNamespace(read=AsyncMock(return_value=b'')),returncode=None)
        process.kill = lambda:setattr(process,'returncode',-9)
        async def wait():process.returncode=0
        process.wait=AsyncMock(side_effect=wait)
        new = {'id':'native:sent','text':'reply','outgoing':True}
        self.reader.read_messages = unittest.mock.Mock(side_effect=[[],[new]])
        prepare = AsyncMock(return_value=None)
        committed = []
        with patch('services.wechat_desktop.asyncio.create_subprocess_exec',AsyncMock(return_value=process)):
            receipt = await self.transport.send_checked('Group','reply','account',prepare,lambda:None,lambda:committed.append(True))
        self.assertTrue(receipt['verified'])
        self.assertEqual(receipt['message_id'],'native:sent')
        self.assertTrue(writes[0]['native_receipt'])
        self.assertEqual(writes[1],{'commit':True,'text':'reply'})
        self.assertEqual(committed,[True])
