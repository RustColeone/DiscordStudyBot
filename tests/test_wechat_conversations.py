import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from providers.wechat_reply import WeChatReplyGenerator
from services import database as db
from services.wechat_conversations import WeChatConversationStore, WeChatReplySupervisor
from services.agent_loop import AgentRun
from services.agent_task import AgentTask


def message(identifier, text="你好", outgoing=False, chat="alice", kind="text"):
    return {"id": identifier, "chat_id": chat, "sender_id": "owner" if outgoing else "alice-id",
            "sender_name": "Owner" if outgoing else "Alice", "text": text,
            "outgoing": outgoing, "kind": kind}


class WeChatConversationStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(db, "DATABASE_PATH", str(Path(directory.name) / "wechat.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = WeChatConversationStore()

    def test_native_binding_is_durable_and_manual_identical_text_is_not_agent_echo(self):
        self.store.pin_native_chat('owner','name:Group','room@chatroom')
        self.assertEqual(WeChatConversationStore().native_bindings('owner'), {'name:Group':'room@chatroom'})
        with self.assertRaises(ValueError):self.store.pin_native_chat('owner','name:Group','other@chatroom')
        self.assertEqual(self.store.native_bindings('other'),{})
        self.store.record('owner','name:Group',message('native:sent',text='same',outgoing=True),source='agent')
        self.assertTrue(self.store.is_agent_echo('owner','name:Group',message('native:sent',text='same',outgoing=True)))
        self.assertFalse(self.store.is_agent_echo('owner','name:Group',message('native:manual',text='same',outgoing=True)))

    def test_native_group_mentions_use_ids_without_nickname_configuration(self):
        self.store.configure('owner','group',whitelisted=True,agent_enabled=True,chat_kind='group')
        self.store.native_mentions = True
        self.assertTrue(self.store.reply_ready('owner','group'))
        self.assertTrue(self.store.addressed('owner','group',{'mentions_me':True}))
        self.assertFalse(self.store.addressed('owner','group',{'mentions_me':False,'text':'@Me hello'}))
        self.assertFalse(self.store.addressed('owner','group',{'text':'@Me hello'}))

    def test_reading_existing_settings_and_messages_does_not_advance_sqlite_sequence(self):
        self.store.get('owner','alice')
        self.store.record('owner','alice',message('existing'))
        with db.get_db() as conn:
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='wechat_conversation_messages'").fetchone()[0]
        for _ in range(5):
            self.store.get('owner','alice')
            self.assertFalse(self.store.record('owner','alice',message('existing')))
        with db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='wechat_conversation_messages'").fetchone()[0],sequence)

    def test_settings_are_durable_scoped_and_opt_in(self):
        self.assertFalse(self.store.get("owner", "alice")["agent_enabled"])
        self.assertEqual(self.store.get("owner", "alice")["notify_mode"], "inherit")
        setting = self.store.configure("owner", "alice", agent_enabled=True, instructions="Keep it short.",
                                       delay_seconds=25, notify_channel="discord-room")
        self.assertEqual(setting["revision"], 1)
        self.assertTrue(WeChatConversationStore().get("owner", "alice")["agent_enabled"])
        self.assertFalse(self.store.get("another-account", "alice")["agent_enabled"])
        self.assertFalse(self.store.get("owner", "bob")["agent_enabled"])
        self.assertEqual(self.store.configure("owner", "alice", agent_enabled=True)["revision"], 1)

    def test_observed_softball_mention_requires_exact_alias_and_explains_rejection(self):
        self.store.configure('owner', '*', mention_aliases=['🎾🎾🎾🎾'])
        self.store.configure('owner', 'group', whitelisted=True, agent_enabled=True, chat_kind='group')
        observed = {**message('test', '@🥎🥎🥎🥎\u2005在？'), 'chat_kind':'group',
                    'mention_names':['🥎🥎🥎🥎']}
        self.assertEqual(self.store.reply_gate('owner', 'group', observed), 'mention_name_mismatch')
        self.assertFalse(self.store.addressed('owner', 'group', observed))
        self.store.configure('owner', '*', mention_aliases=['🥎🥎🥎🥎'])
        self.assertEqual(self.store.reply_gate('owner', 'group', observed), 'accepted')
        self.assertTrue(self.store.addressed('owner', 'group', observed))
        other = {**observed, 'mention_names':['🎾🎾🎾🎾']}
        self.assertFalse(self.store.addressed('owner', 'group', other))
        self.store.configure('owner', 'group', whitelisted=False)
        self.assertEqual(self.store.reply_gate('owner', 'group', observed), 'not_whitelisted')

    def test_settings_reject_invalid_and_unknown_values(self):
        for setting in ({"delay_seconds": 1}, {"agent_enabled": "true"}, {"max_reply_chars": 2001},
                        {"instructions": "x" * 4001}, {"notify_channel": ""}, {"notify_mode": "invalid"},
                        {"arbitrary": 1}):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                self.store.configure("owner", "alice", **setting)

    def test_same_text_distinct_message_ids_remains_distinct_and_history_is_bounded(self):
        self.assertTrue(self.store.record("owner", "alice", message("one")))
        self.assertFalse(self.store.record("owner", "alice", message("one")))
        self.assertTrue(self.store.record("owner", "alice", message("two")))
        self.assertEqual(len(self.store.history("owner", "alice")), 2)
        self.assertEqual(self.store.history("other", "alice"), [])
        for index in range(45):
            self.store.record("owner", "alice", message(str(index), text="x" * 1800))
        history = self.store.history("owner", "alice", limit=50)
        self.assertLess(sum(len(json.dumps(item, ensure_ascii=False)) for item in history), 12000)
        self.assertEqual(history[-1]["id"], "44")

    def test_notify_mode_migrates_existing_table_and_per_chat_opt_out_persists(self):
        with db.get_db() as conn:
            conn.execute("""CREATE TABLE wechat_conversations (
                account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                agent_enabled INTEGER NOT NULL DEFAULT 0, instructions TEXT NOT NULL DEFAULT '',
                delay_seconds REAL NOT NULL DEFAULT 20, max_reply_chars INTEGER NOT NULL DEFAULT 500,
                revision INTEGER NOT NULL DEFAULT 0, notify_channel TEXT,
                PRIMARY KEY (account_id,chat_id))""")
            conn.execute("INSERT INTO wechat_conversations (account_id,chat_id) VALUES ('owner','alice')")
        self.assertEqual(self.store.get("owner", "alice")["notify_mode"], "inherit")
        self.store.configure("owner", "*", notify_mode="on", notify_channel="discord")
        self.store.configure("owner", "alice", notify_mode="off")
        self.assertEqual(WeChatConversationStore().get("owner", "alice")["notify_mode"], "off")
        self.assertEqual(self.store.get("owner", "*")["notify_mode"], "on")


class WeChatReplySupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_expanding_native_history_cannot_cancel_current_mention_or_replay_old_messages(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['Me'])
        baseline = {**message('native:50','old visible'),'position':[50,50]}
        await self.supervisor.baseline('owner','alice',[baseline])
        trigger = {**message('native:84','@Me\u2005question'),'position':[84,84],'server_id':'123'}
        old_manual = {**message('native:1','old reply',outgoing=True),'position':[1,1]}
        old_mention = {**message('native:2','@Me\u2005old question'),'position':[2,2]}
        old_media = {**message('native:3','old media',kind='image'),'position':[3,3]}
        chatter = {**message('native:85','another person speaking'),'position':[85,85],'sender_id':'other'}
        self.transport.read_messages.return_value = [old_manual,old_mention,old_media,baseline,trigger,chatter]
        await self.supervisor.observe('owner','alice',[baseline,trigger])
        await self.settle()
        self.transport.send.assert_awaited_once()
        self.assertEqual(self.generator.await_args.args[0]['reply_target']['id'],'native:84')
        incoming = [call.args[0]['message']['id'] for call in self.notify.await_args_list if call.args[0]['type']=='incoming']
        self.assertNotIn('native:2',incoming)
        self.assertEqual(self.supervisor._positions[('owner','alice')],(85,85))

    async def test_group_mentions_queue_separately_and_each_quote_keeps_its_trigger(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['Me'])
        await self.supervisor.baseline('owner','alice',[])
        first = {**message('one','@Me\u2005first'),'sender_id':'person-one','server_id':'101'}
        second = {**message('two','@Me\u2005second'),'sender_id':'person-two','server_id':'102'}
        started,finish = asyncio.Event(),asyncio.Event()
        contexts,targets = [],[]
        async def generate(context):
            contexts.append(context)
            if len(contexts)==1:
                started.set();await finish.wait()
            return 'answer '+context['messages'][0]['id']
        async def send_checked(chat,text,expected_account,prepare,authorize,on_commit,reply_to):
            decision=await prepare({'state':'ready','account_id':'owner'},[first,second])
            self.assertIsNone(decision)
            authorize();on_commit();targets.append(reply_to['id'])
            return {'verified':True,'message_id':'native:sent-'+reply_to['id']}
        self.generator.side_effect=generate
        self.transport.atomic_checked_sends=True
        self.transport.supports_quoted_replies=True
        self.transport.send_checked=AsyncMock(side_effect=send_checked)
        await self.supervisor.observe('owner','alice',[first])
        await started.wait()
        await self.supervisor.observe('owner','alice',[second])
        finish.set();await self.settle()
        self.assertEqual(targets,['one','two'])
        self.assertEqual([[m['id'] for m in c['messages']] for c in contexts],[['one'],['two']])
        self.assertNotIn('one',[m['id'] for m in contexts[1]['history']])

    async def restarted(self, **options):
        restarted = WeChatReplySupervisor(self.transport, self.store, self.notify, self.generator, **options)
        restarted._sleep = AsyncMock()
        self.addAsyncCleanup(restarted.close)
        return restarted

    async def test_recent_message_arriving_while_stopped_is_answered_after_restart(self):
        await self.enable()
        seen = {**message('native:10', 'before stop'), 'position': [10, 10], 'timestamp': int(time.time()) - 300}
        await self.supervisor.baseline('owner', 'alice', [seen])
        await self.supervisor.close()
        missed = {**message('native:11', '说句话'), 'position': [11, 11], 'timestamp': int(time.time()) - 60}
        restarted = await self.restarted()
        with patch('builtins.print') as output:
            await restarted.observe('owner', 'alice', [seen, missed])
        await asyncio.wait_for(restarted._tasks[('owner', 'alice')], timeout=2)
        self.generator.assert_awaited_once()
        self.assertEqual([m['id'] for m in self.generator.await_args.args[0]['messages']], ['native:11'])
        self.transport.send.assert_awaited_once()
        self.assertTrue(any('catch_up_messages' in call.args[0] for call in output.call_args_list))
        self.assertFalse(any('说句话' in call.args[0] for call in output.call_args_list))
        self.assertEqual(self.store.read_cursor('owner', 'alice')[0], (11, 11))
        # The same message is not answered again by a later restart.
        again = await self.restarted()
        await again.observe('owner', 'alice', [seen, missed])
        self.assertNotIn(('owner', 'alice'), again._tasks)
        self.generator.assert_awaited_once()

    async def test_restart_catch_up_is_bounded_by_age_settings_revision_and_first_start(self):
        await self.enable()
        seen = {**message('native:10'), 'position': [10, 10], 'timestamp': int(time.time()) - 3000}
        await self.supervisor.baseline('owner', 'alice', [seen])
        stale = {**message('native:11', 'old question'), 'position': [11, 11], 'timestamp': int(time.time()) - 1200}
        await (await self.restarted()).observe('owner', 'alice', [seen, stale])
        self.generator.assert_not_awaited()
        # A settings change after the cursor means the chat was not reply-ready under the same policy.
        self.store.configure('owner', 'alice', instructions='changed while stopped')
        recent = {**message('native:12'), 'position': [12, 12], 'timestamp': int(time.time())}
        await (await self.restarted()).observe('owner', 'alice', [seen, stale, recent])
        self.generator.assert_not_awaited()
        # No durable cursor (first start, or catch-up disabled) remains a plain baseline.
        await self.supervisor.configure('owner', 'bob', agent_enabled=True, whitelisted=True, chat_kind='direct')
        latest = {**message('native:20', chat='bob'), 'position': [20, 20], 'timestamp': int(time.time())}
        await (await self.restarted()).observe('owner', 'bob', [latest])
        newer = {**message('native:21', chat='bob'), 'position': [21, 21], 'timestamp': int(time.time())}
        await (await self.restarted(catch_up_seconds=0)).observe('owner', 'bob', [latest, newer])
        self.generator.assert_not_awaited()
        self.transport.send.assert_not_awaited()

    async def test_transport_verified_media_is_own_echo_not_unsupported_activity(self):
        await self.supervisor.configure('owner', 'alice', agent_enabled=True, whitelisted=True,
                                        chat_kind='group', notify_channel='discord-room')
        await self.supervisor.baseline('owner', 'alice', [])
        self.transport.own_message_ids = ['native:own-video']
        own = {**message('native:own-video', '[Video]', outgoing=True, kind='video'), 'position': [5, 5]}
        await self.supervisor.observe('owner', 'alice', [own])
        self.assertNotIn('unsupported_activity', [e['code'] for e in self.supervisor.status()['errors']])
        self.assertFalse([c for c in self.notify.await_args_list if c.args[0]['type'] == 'reply_skipped'])
        foreign = {**message('native:manual-video', '[Video]', outgoing=True, kind='video'), 'position': [6, 6]}
        await self.supervisor.observe('owner', 'alice', [own, foreign])
        self.assertTrue([c for c in self.notify.await_args_list if c.args[0]['type'] == 'reply_skipped'])

    async def test_real_new_manual_reply_still_cancels_with_native_cursor(self):
        await self.enable()
        baseline={**message('native:old'),'position':[10,10]}
        await self.supervisor.baseline('owner','alice',[baseline])
        trigger={**message('native:new'),'position':[11,11]}
        manual={**message('native:manual','human reply',outgoing=True),'position':[12,12]}
        self.transport.read_messages.return_value=[baseline,trigger,manual]
        await self.supervisor.observe('owner','alice',[trigger])
        await self.settle()
        self.transport.send.assert_not_awaited()

    async def test_group_waiting_for_owner_keeps_later_mentions_queued_until_resume(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['Me'])
        await self.supervisor.baseline('owner','alice',[])
        first={**message('one','@Me\u2005first'),'sender_id':'one'}
        second={**message('two','@Me\u2005second'),'sender_id':'two'}
        contexts=[]
        class WaitingAgent:
            state=None
            def reset(self,key):self.state=None
            def pending(self,key):return self.state
            async def run(self,context,publish,valid):
                contexts.append(context)
                self.state={'status':'awaiting_confirmation'} if len(contexts)==1 else None
        self.supervisor.agent=WaitingAgent()
        await self.supervisor.observe('owner','alice',[first]);await self.settle()
        await self.supervisor.observe('owner','alice',[second]);await self.settle()
        self.assertEqual(len(contexts),1)
        self.assertEqual(self.supervisor.status()['pending_messages'],1)
        self.supervisor.resume('owner','alice');await self.settle()
        self.assertEqual([c['reply_target']['id'] for c in contexts],['one','one','two'])
        self.assertEqual(contexts[0]['reply_request_id'],contexts[1]['reply_request_id'])

    async def test_group_clarification_belongs_to_original_request_and_other_mentions_wait(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['Me'])
        await self.supervisor.baseline('owner','alice',[])
        first={**message('one','@Me\u2005first'),'sender_id':'person-one'}
        other={**message('two','@Me\u2005another request'),'sender_id':'person-two'}
        answer={**message('three','@Me\u2005clarification'),'sender_id':'person-one'}
        contexts=[]
        class WaitingAgent:
            state=None
            def reset(self,key):self.state=None
            def pending(self,key):return self.state
            async def run(self,context,publish,valid):
                contexts.append(context)
                self.state={'status':'needs_input'} if len(contexts)==1 else None
        self.supervisor.agent=WaitingAgent()
        await self.supervisor.observe('owner','alice',[first]);await self.settle()
        await self.supervisor.observe('owner','alice',[other]);await self.settle()
        self.assertEqual(len(contexts),1)
        await self.supervisor.observe('owner','alice',[answer]);await self.settle()
        self.assertEqual([c['reply_target']['id'] for c in contexts],['one','three','two'])
        self.assertEqual(contexts[0]['reply_request_id'],contexts[1]['reply_request_id'])
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(db, "DATABASE_PATH", str(Path(directory.name) / "wechat.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = WeChatConversationStore()
        self.transport = SimpleNamespace(status=AsyncMock(return_value={"state": "ready", "account_id": "owner", "name": "Owner"}),
            read_messages=AsyncMock(return_value=[]), send=AsyncMock(return_value={"verified": True, "message_id": "sent-one"}))
        self.generator = AsyncMock(return_value="好呀。")
        self.notify = AsyncMock()
        self.supervisor = WeChatReplySupervisor(self.transport, self.store, self.notify, self.generator)
        self.supervisor._sleep = AsyncMock()
        self.addAsyncCleanup(self.supervisor.close)

    async def enable(self, chat="alice"):
        await self.supervisor.configure("owner", chat, agent_enabled=True,whitelisted=True,chat_kind='direct',notify_channel="discord-room")
        await self.supervisor.baseline("owner", chat, [message("old", chat=chat)])

    async def settle(self, chat="alice"):
        task = self.supervisor._tasks.get(("owner", chat))
        if task is not None:
            await asyncio.wait_for(task, timeout=2)

    async def test_incoming_notification_survives_ambiguous_activity_cancelling_reply(self):
        await self.enable()
        await self.supervisor.observe('owner', 'alice', [message('old'), message('new', 'new text'),
            message('media', 'Unsupported media', outgoing=None, kind='image')])
        incoming = [call.args[0] for call in self.notify.await_args_list if call.args[0]['type'] == 'incoming']
        self.assertEqual([event['message']['text'] for event in incoming], ['new text'])
        self.transport.send.assert_not_awaited()

    async def test_group_mention_rejection_is_visible_without_generation_or_raw_content_in_log(self):
        await self.supervisor.configure('owner', 'alice', whitelisted=True, agent_enabled=True,
                                        chat_kind='group', mention_aliases=['🎾🎾🎾🎾'])
        await self.supervisor.baseline('owner', 'alice', [])
        observed = {**message('test', '@🥎🥎🥎🥎\u2005private test'), 'chat_kind':'group',
                    'mention_names':['🥎🥎🥎🥎']}
        with patch('builtins.print') as output:
            await self.supervisor.observe('owner', 'alice', [observed])
        self.assertEqual(self.supervisor.status()['routing'][0]['decisions'], {'mention_name_mismatch':1})
        self.assertIn('mention_name_mismatch', output.call_args.args[0])
        self.assertNotIn('private test', output.call_args.args[0])
        self.generator.assert_not_awaited()
        self.transport.send.assert_not_awaited()

    async def test_baseline_and_disabled_chats_never_reply(self):
        self.store.configure("owner", "alice", agent_enabled=True)
        await self.supervisor.observe("owner", "alice", [message("old")])
        await self.supervisor.observe("owner", "bob", [message("old", chat="bob")])
        await self.supervisor.observe("owner", "bob", [message("new", chat="bob")])
        self.generator.assert_not_awaited()
        self.transport.send.assert_not_awaited()

    async def test_coalesced_new_messages_generate_once_and_verified_reply_is_saved(self):
        await self.enable()
        await self.supervisor.observe("owner", "alice", [message("old"), message("new")])
        await self.supervisor.observe("owner", "alice", [message("new"), message("new-two", "还有一件事")])
        await self.settle()
        self.generator.assert_awaited_once()
        context = self.generator.await_args.args[0]
        self.assertEqual([item["id"] for item in context["messages"]], ["new", "new-two"])
        self.transport.send.assert_awaited_once_with("alice", "好呀。")
        history = self.store.history("owner", "alice")
        self.assertEqual(history[-1]["text"], "好呀。")
        self.assertTrue(history[-1]["outgoing"])
        self.assertEqual(self.supervisor.status()["errors"], [])

    async def test_idle_reply_does_not_wait_full_configured_send_interval(self):
        await self.enable()
        self.store.configure('owner', 'alice', delay_seconds=75)
        await self.supervisor.baseline('owner', 'alice', [])
        await self.supervisor.observe('owner', 'alice', [message('fresh')])
        await self.settle()
        self.generator.assert_awaited_once()
        self.transport.send.assert_awaited_once()
        waits = [call.args[0] for call in self.supervisor._sleep.await_args_list]
        self.assertEqual(waits[0], 1)
        self.assertTrue(all(wait <= 1 for wait in waits), waits)

    async def test_generation_overlaps_cooldown_but_delivery_waits_for_chat_interval(self):
        await self.enable()
        self.store.configure('owner', 'alice', delay_seconds=75)
        await self.supervisor.baseline('owner', 'alice', [])
        self.supervisor._last_chat_send[('owner','alice')] = time.monotonic()
        waiting, release = asyncio.Event(), asyncio.Event()
        waits = []
        async def sleep(seconds):
            waits.append(seconds)
            if seconds > 1:
                waiting.set()
                await release.wait()
        self.supervisor._sleep = sleep
        await self.supervisor.observe('owner', 'alice', [message('fresh')])
        await asyncio.wait_for(waiting.wait(), 1)
        self.generator.assert_awaited_once()
        self.transport.send.assert_not_awaited()
        self.assertGreater(waits[-1], 70)
        release.set()
        await self.settle()
        self.transport.send.assert_awaited_once()

    async def test_manual_reply_observed_before_send_cancels_generated_response(self):
        await self.enable()
        self.transport.read_messages.return_value = [message("new"), message("manual", "我自己回", outgoing=True)]
        await self.supervisor.observe("owner", "alice", [message("new")])
        await self.settle()
        self.generator.assert_awaited_once()
        self.transport.send.assert_not_awaited()

    async def test_configuration_change_and_manual_send_cancel_inflight_generation(self):
        for operation in ("disable", "manual"):
            with self.subTest(operation=operation):
                await self.enable()
                started, finish = asyncio.Event(), asyncio.Event()
                async def generate(context):
                    started.set()
                    await finish.wait()
                    return "stale"
                self.generator.side_effect = generate
                await self.supervisor.observe("owner", "alice", [message("new-" + operation)])
                await asyncio.wait_for(started.wait(), 1)
                if operation == "disable":
                    await self.supervisor.configure("owner", "alice", agent_enabled=False)
                else:
                    await self.supervisor.manual_sent("owner", "alice")
                finish.set()
                self.transport.send.assert_not_awaited()

    async def test_unverified_send_is_not_saved_or_retried(self):
        await self.enable()
        self.transport.send.return_value = {"verified": False}
        await self.supervisor.observe("owner", "alice", [message("new")])
        await self.settle()
        await self.supervisor.observe("owner", "alice", [message("new")])
        self.transport.send.assert_awaited_once()
        self.assertFalse(any(item["outgoing"] for item in self.store.history("owner", "alice")))
        self.assertEqual(self.supervisor.status()["errors"][0]["code"], "send_unverified")

    async def test_notification_callback_routes_inherited_destination_without_chat_override(self):
        await self.supervisor.baseline("owner", "alice", [])
        await self.supervisor.observe("owner", "alice", [message("new")])
        self.notify.assert_awaited_once()
        event = self.notify.await_args.args[0]
        self.assertEqual(event["type"], "incoming")
        self.assertIsNone(event["notify_channel"])
        self.transport.send.assert_not_awaited()

    async def test_arrival_during_generation_regenerates_one_combined_reply(self):
        await self.enable()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0
        async def generate(context):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await finish.wait()
                return "old reply"
            return "combined reply"
        self.generator.side_effect = generate
        await self.supervisor.observe("owner", "alice", [message("new-one")])
        await asyncio.wait_for(started.wait(), 1)
        await self.supervisor.observe("owner", "alice", [message("new-one"), message("new-two")])
        finish.set()
        await self.settle()
        self.assertEqual(self.generator.await_count, 2)
        self.transport.send.assert_awaited_once_with("alice", "combined reply")
        self.assertEqual([item["id"] for item in self.generator.await_args.args[0]["messages"]],
                         ["new-one", "new-two"])

    async def test_new_arrival_seen_at_final_read_defers_stale_reply(self):
        await self.enable()
        self.generator.side_effect = ["old reply", "combined reply"]
        self.transport.read_messages.return_value = [message("new-one"), message("new-two")]
        await self.supervisor.observe("owner", "alice", [message("new-one")])
        await self.settle()
        self.assertEqual(self.generator.await_count, 2)
        self.transport.send.assert_awaited_once_with("alice", "combined reply")

    async def test_atomic_delivery_rechecks_manual_reply_and_account_before_commit(self):
        await self.enable()
        self.transport.atomic_checked_sends = True
        committed=[]
        async def checked(chat, text, *, expected_account, prepare, authorize, on_commit):
            decision = await prepare({'state':'ready','account_id':'owner'},
                [message('fresh'), message('manual', 'Already answered', outgoing=True)])
            if decision is not None: return decision
            authorize();on_commit();committed.append(text)
            return {'verified':True,'message_id':'sent'}
        self.transport.send_checked = AsyncMock(side_effect=checked)
        await self.supervisor.observe('owner','alice',[message('fresh')])
        await self.settle()
        self.assertEqual(committed,[])
        self.transport.send.assert_not_awaited()
        self.transport.status.assert_not_awaited()
        self.assertFalse(any(m.get('id')=='sent' for m in self.store.history('owner','alice')))

    async def test_atomic_delivery_merges_new_input_before_one_verified_commit(self):
        await self.enable()
        self.transport.atomic_checked_sends = True
        self.generator.side_effect = ['stale','combined']
        committed=[]
        async def checked(chat, text, *, expected_account, prepare, authorize, on_commit):
            decision = await prepare({'state':'ready','account_id':'owner'},[message('one'),message('two')])
            if decision is not None: return decision
            authorize();on_commit();committed.append(text)
            return {'verified':True,'message_id':'committed'}
        self.transport.send_checked = AsyncMock(side_effect=checked)
        await self.supervisor.observe('owner','alice',[message('one')])
        await self.settle()
        self.assertEqual(committed,['combined'])
        self.assertEqual(self.generator.await_count,2)
        self.assertEqual(self.store.history('owner','alice')[-1]['id'],'committed')

    async def test_incoming_after_a_manual_reply_in_same_batch_can_be_answered(self):
        await self.enable()
        await self.supervisor.observe("owner", "alice", [message("before", "first question"),
            message("manual", "answered", outgoing=True), message("after", "new question")])
        await self.settle()
        self.assertEqual([item["id"] for item in self.generator.await_args.args[0]["messages"]], ["after"])
        self.transport.send.assert_awaited_once()

    async def test_disable_during_started_send_preserves_receipt_without_retry(self):
        await self.enable()
        started, finish = asyncio.Event(), asyncio.Event()
        async def send(chat_id, text):
            started.set()
            await finish.wait()
            return {"verified": True, "message_id": "sent-despite-disable"}
        self.transport.send.side_effect = send
        await self.supervisor.observe("owner", "alice", [message("new")])
        await asyncio.wait_for(started.wait(), 1)
        disabling = asyncio.create_task(self.supervisor.configure("owner", "alice", agent_enabled=False))
        await asyncio.sleep(0)
        self.assertFalse(disabling.done())
        finish.set()
        await asyncio.wait_for(disabling, 1)
        self.transport.send.assert_awaited_once()
        self.assertEqual(self.store.history("owner", "alice")[-1]["id"], "sent-despite-disable")

    async def test_unknown_direction_cancels_queued_or_generating_reply(self):
        for stage in ("queued", "generating"):
            with self.subTest(stage=stage):
                await self.enable()
                started, finish = asyncio.Event(), asyncio.Event()
                async def block(*args):
                    started.set()
                    await finish.wait()
                    return "old reply"
                self.supervisor._sleep = block if stage == "queued" else AsyncMock()
                self.generator.side_effect = block if stage == "generating" else None
                await self.supervisor.observe("owner", "alice", [message("new-" + stage)])
                await asyncio.wait_for(started.wait(), 1)
                unknown = {**message("unknown-" + stage, text="[image]", kind="unsupported"), "outgoing": None}
                await self.supervisor.observe("owner", "alice", [unknown])
                finish.set()
                self.transport.send.assert_not_awaited()
                self.assertEqual(self.supervisor.status()["pending_messages"], 0)
                self.assertEqual(self.supervisor.status()["errors"][0]["code"], "unsupported_activity")
                self.assertEqual(self.notify.await_args.args[0]["type"], "reply_skipped")
                self.assertIsNone(self.store.history("owner", "alice")[-1]["outgoing"])

    async def test_unknown_direction_seen_at_final_read_cancels_reply(self):
        await self.enable()
        unknown = {**message("unknown", "uncertain activity"), "outgoing": None}
        self.transport.read_messages.return_value = [message("new"), unknown]
        await self.supervisor.observe("owner", "alice", [message("new")])
        await self.settle()
        self.generator.assert_awaited_once()
        self.transport.send.assert_not_awaited()

    async def test_close_waits_for_started_send_and_keeps_verified_receipt(self):
        await self.enable()
        started, finish = asyncio.Event(), asyncio.Event()
        async def send(chat_id, text):
            started.set()
            await finish.wait()
            return {"verified": True, "message_id": "sent-before-shutdown"}
        self.transport.send.side_effect = send
        await self.supervisor.observe("owner", "alice", [message("new")])
        await asyncio.wait_for(started.wait(), 1)
        closing = asyncio.create_task(self.supervisor.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        finish.set()
        await asyncio.wait_for(closing, 1)
        self.transport.send.assert_awaited_once()
        self.assertEqual(self.store.history("owner", "alice")[-1]["id"], "sent-before-shutdown")
        self.assertTrue(self.supervisor.status()["closed"])

    async def test_different_account_never_receives_pending_reply(self):
        await self.enable()
        self.transport.status.return_value = {"state": "ready", "account_id": "other-owner"}
        await self.supervisor.observe("owner", "alice", [message("new")])
        await self.settle()
        self.transport.send.assert_not_awaited()
        self.assertEqual(self.supervisor.status()["errors"][0]["code"], "account_not_ready")

    async def test_chat_workers_progress_independently_and_use_account_throttle(self):
        await self.enable("alice")
        await self.enable("bob")
        blocked = asyncio.Event()
        started = asyncio.Event()
        async def generate(context):
            if context["chat_id"] == "alice":
                started.set()
                await blocked.wait()
            return "hi"
        self.generator.side_effect = generate
        await self.supervisor.observe("owner", "alice", [message("new-a")])
        await asyncio.wait_for(started.wait(), 1)
        await self.supervisor.observe("owner", "bob", [message("new-b", chat="bob")])
        await self.settle("bob")
        self.transport.send.assert_awaited_once_with("bob", "hi")
        blocked.set()
        await self.settle("alice")
        self.assertEqual(self.transport.send.await_count, 2)
        self.assertTrue(any(call.args[0] > 0 for call in self.supervisor._sleep.await_args_list))

    async def test_nontext_and_agent_echo_never_create_another_reply(self):
        await self.enable()
        await self.supervisor.observe("owner", "alice", [message("picture", kind="image")])
        self.generator.assert_not_awaited()
        self.store.record("owner", "alice", message("sent-id", "hi", outgoing=True), source="agent")
        await self.supervisor.observe("owner", "alice", [message("echo-ui", "hi", outgoing=True)])
        self.generator.assert_not_awaited()

    async def test_generation_failure_never_sends_error_text(self):
        await self.enable()
        self.generator.return_value = None
        await self.supervisor.observe("owner", "alice", [message("new")])
        await self.settle()
        self.transport.send.assert_not_awaited()
        self.assertEqual(self.supervisor.status()["errors"][0]["code"], "generation_failed_or_skipped")

    async def test_group_only_exact_account_mention_triggers_generation(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['🎾🎾🎾🎾'])
        await self.supervisor.baseline('owner','alice',[])
        await self.supervisor.observe('owner','alice',[message('chatter','hello everyone')])
        await self.supervisor.observe('owner','alice',[message('other','@someone\u2005hello')])
        self.generator.assert_not_awaited()
        addressed=message('me','@🎾🎾🎾🎾\u2005hello')
        await self.supervisor.observe('owner','alice',[addressed])
        await self.settle()
        self.generator.assert_awaited_once()
        self.assertEqual([m['id'] for m in self.generator.await_args.args[0]['messages']],['me'])
        self.assertNotIn('hello everyone',[m['text'] for m in self.generator.await_args.args[0]['history']])

    async def test_unmentioned_group_chatter_notifies_without_steering_inflight_work(self):
        await self.enable()
        await self.supervisor.configure('owner','alice',chat_kind='group',mention_aliases=['Me'])
        await self.supervisor.baseline('owner','alice',[])
        started,finish=asyncio.Event(),asyncio.Event()
        async def generate(context):
            started.set();await finish.wait();return 'done'
        self.generator.side_effect=generate
        await self.supervisor.observe('owner','alice',[message('mention','@Me\u2005do work')])
        await started.wait()
        await self.supervisor.observe('owner','alice',[message('chatter','other conversation')])
        finish.set();await self.settle()
        self.generator.assert_awaited_once()
        self.assertTrue(any(call.args[0]['type']=='incoming' and call.args[0]['message'].get('id')=='chatter'
                            for call in self.notify.await_args_list))

    async def test_whitelist_revocation_cancels_pending_generation(self):
        await self.enable()
        started=asyncio.Event()
        async def generate(context):
            started.set();await asyncio.Event().wait()
        self.generator.side_effect=generate
        await self.supervisor.observe('owner','alice',[message('request')])
        await started.wait()
        await self.supervisor.configure('owner','alice',whitelisted=False)
        self.transport.send.assert_not_awaited()

    async def test_native_group_metadata_cannot_bypass_mentions_with_direct_setting(self):
        await self.enable()
        await self.supervisor.observe('owner','alice',[{**message('not-addressed'),'chat_kind':'group','mention_names':[]}])
        self.generator.assert_not_awaited()


class WeChatReplyProviderTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {"account_id": "owner", "chat_id": "alice", "settings": {"instructions": "Be friendly.",
                "max_reply_chars": 500}, "history": [], "messages": [message("new")]}

    async def test_persona_and_owner_instructions_used_without_room_or_tools(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"reply":"你好呀。"}'))])))))
        generator = WeChatReplyGenerator(config={}, client=client)
        self.assertEqual(await generator(self.context()), "你好呀。")
        kwargs = client.chat.completions.create.await_args.kwargs
        self.assertEqual(kwargs["timeout"], 35)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("tools", kwargs)
        self.assertIn("CETACEA_LOLI", kwargs["messages"][0]["content"])
        self.assertIn("Be friendly.", kwargs["messages"][0]["content"])
        self.assertNotIn("Luna", kwargs["messages"][0]["content"])

    async def test_luna_switch_preserves_persona_and_structured_tool_actions(self):
        create = AsyncMock(return_value=SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=json.dumps({
                'status':'continue','message':'我去查一下。',
                'actions':[{'tool':'inspect','arguments':{}}]})))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        generator = WeChatReplyGenerator(config={'WECHAT_BRIDGE_REPLY_PROVIDER':'luna'}, client=client)
        context = self.context()
        context['delivery_policy'] = {'max_file_mb':100,'source':'bridge_policy','platform_limit_mb':None}
        state = AgentRun(AgentTask('check'))
        state.visible_turns.append('I already said hello.')
        step = await generator.step(context, state, [{'name':'inspect','parameters':{}}])
        self.assertEqual(generator.model, 'gpt-6-luna')
        self.assertEqual(generator.base_url, 'https://api.openai.com/v1')
        self.assertEqual(step.actions, [{'tool':'inspect','arguments':{}}])
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs['max_completion_tokens'],4096)
        self.assertEqual(kwargs['reasoning_effort'],'low')
        self.assertEqual(kwargs['response_format'],{'type':'json_object'})
        self.assertNotIn('max_tokens',kwargs)
        self.assertNotIn('temperature',kwargs)
        self.assertIn('CETACEA_LOLI',kwargs['messages'][0]['content'])
        self.assertIn('inspect',kwargs['messages'][1]['content'])
        data=json.loads(kwargs['messages'][1]['content'])
        self.assertEqual(data['work']['visible_turns'], ['I already said hello.'])
        self.assertEqual(data['delivery_policy']['source'], 'bridge_policy')

    async def test_provider_switch_supports_openrouter_model_and_invalid_config_fails(self):
        with patch.dict('os.environ', {'AGENT_LUNA_BACKEND':'openrouter',
                                      'OPENROUTER_LUNA_MODEL':'openai/gpt-6-luna'}):
            create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
                finish_reason='stop',message=SimpleNamespace(content='{"reply":"你好。"}'))]))
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            generator=WeChatReplyGenerator(config={'WECHAT_BRIDGE_REPLY_PROVIDER':'luna'},client=client)
            self.assertEqual(await generator(self.context()),'你好。')
            kwargs=create.await_args.kwargs
            self.assertEqual(kwargs['model'],'openai/gpt-6-luna')
            self.assertEqual(kwargs['extra_body'],{'reasoning':{'effort':'low','exclude':True}})
        with self.assertRaises(ValueError):
            WeChatReplyGenerator(config={'WECHAT_BRIDGE_REPLY_PROVIDER':'unknown'})

    async def test_malformed_truncated_and_error_outputs_never_become_replies(self):
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        generator = WeChatReplyGenerator(config={}, client=client)
        for raw, reason in (("raw provider output", "stop"), ('{"reply":"partial"}', "length"),
                            ('{"reply":"hello","extra":"secret"}', "stop"),
                            ('{"reply":"<diary_updates>private"}', "stop")):
            create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,
                                                message=SimpleNamespace(content=raw))])
            self.assertIsNone(await generator(self.context()))
        create.side_effect = RuntimeError("private provider detail")
        self.assertIsNone(await generator(self.context()))

    async def test_structured_work_step_rejects_non_object_and_truncated_output(self):
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        generator = WeChatReplyGenerator(config={}, client=client)
        state = AgentRun(AgentTask('check'))
        for raw, reason in (('[{"status":"complete","message":"hi","actions":[]}]', 'stop'),
                            ('{"status":"complete","message":"hi","actions":[]}', 'length')):
            create.return_value = SimpleNamespace(usage=None, choices=[SimpleNamespace(
                finish_reason=reason, message=SimpleNamespace(content=raw))])
            with self.assertRaises(ValueError):
                await generator.step(self.context(), state, [])
        create.return_value = SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content='{"status":"complete","message":"你好呀。","actions":[]}'))])
        self.assertEqual((await generator.step(self.context(), state, [])).message, '你好呀。')


if __name__ == "__main__":
    unittest.main()
