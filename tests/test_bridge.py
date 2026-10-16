import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.bot_types import BotResponse
from bridges.example_bridge import ExampleBridge
from bridges.wechat_bridge import WeChatBridge
from features.feature_bridge import BridgeFeature
from parsers.bridge_parser import parse_bridge_command
from parsers.bridge_commands import COMMANDS
from features.feature_help import HelpFeature
from services import database as db
from services.wechat_conversations import WeChatConversationStore


class BridgeParserTests(unittest.TestCase):
    def test_catalogue_actions_parse_and_help_tracks_registered_factories(self):
        feature = BridgeFeature()
        feature.register('custom', ExampleBridge)
        self.assertIn('example, wechat, custom', feature.help_text())
        for action, (flags, syntax, _) in COMMANDS.items():
            for flag in flags:
                suffix = ' hello' if action == 'send' else ' --task exact' if action == 'approve' else ''
                command = parse_bridge_command('$bridge wechat ' + flag + suffix)
                self.assertFalse(command.errors, (flag, command.errors))
                self.assertEqual(command.action, action)
            self.assertIn(syntax, feature.help_text())

    def test_repeatable_mention_aliases_and_notification_destination(self):
        command = parse_bridge_command('$bridge wechat --whitelist add --chat "Group" --kind group --mention "🎾🎾🎾🎾" --mention "Other Name"')
        self.assertFalse(command.errors)
        self.assertEqual(command.mention_aliases, ['🎾🎾🎾🎾', 'Other Name'])
        self.assertEqual(parse_bridge_command('$bridge wechat --notify on --destination dm').destination, 'dm')
        for text in ('$bridge --status --destination dm', '$bridge --notify on --destination other',
                     '$bridge --send hi --mention Nick', '$bridge --whitelist add --kind other'):
            self.assertTrue(parse_bridge_command(text).errors)

    def test_named_bridge_preserves_unicode_quotes_and_explicit_recipient(self):
        value = parse_bridge_command('$bridge wechat --send "你好，Alice！" --to "Alice Zhang"')
        self.assertEqual((value.bridge_type, value.action, value.message, value.target),
                         ("wechat", "send", "你好，Alice！", "Alice Zhang"))
        self.assertFalse(value.errors)

    def test_legacy_commands_and_independent_chat_settings(self):
        self.assertEqual(parse_bridge_command("$bridge -i").action, "init")
        self.assertEqual(parse_bridge_command("$bridge --listen on").mode, "on")
        value = parse_bridge_command('$bridge wechat --agent on --chat "name:Alice" --delay 25 --instructions "Be brief."')
        self.assertEqual(value.target, "name:Alice")
        self.assertEqual(value.delay_seconds, 25)
        self.assertEqual(value.instructions, "Be brief.")

    def test_invalid_limits_multiple_actions_and_missing_values_are_errors(self):
        for text in ("$bridge --messages --limit 101", "$bridge --status --send hi", "$bridge --send", "$bridge --to",
                     "$bridge --notify maybe", '$bridge --send "unterminated'):
            self.assertTrue(parse_bridge_command(text).errors, text)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = patch.object(db, "DATABASE_PATH", str(Path(self.directory.name) / "bridge.db"))
        self.database.start()
        self.addCleanup(self.database.stop)
        self.app = SimpleNamespace(platform=SimpleNamespace(send_channel_message=AsyncMock()))
        self.message = SimpleNamespace(content="", author_id="owner", channel_id="discord-test")
        self.transport = SimpleNamespace(
            status=AsyncMock(return_value={"state": "ready", "account_id": "account", "name": "Owner"}),
            login=AsyncMock(return_value={"state": "logged_out", "path": "/tmp/qr.png"}),
            resolve=AsyncMock(return_value={"id": "alice", "name": "Alice"}),
            list_chats=AsyncMock(return_value=[{"id": "alice", "name": "Alice", "unread": 0}]),
            read_messages=AsyncMock(return_value=[]), send=AsyncMock(return_value={"verified": True, "message_id": "sent-1"}),
            confirm_login=AsyncMock(), close=AsyncMock())
        self.bridge = WeChatBridge({"WECHAT_BRIDGE_OWNER_ID": "owner"}, self.transport,
                                   WeChatConversationStore(), AsyncMock(return_value="Hi"))
        self.bridge.attach(self.app)
        self.bridge.initialize = AsyncMock(return_value=True)
        self.addAsyncCleanup(self.bridge.close)

    async def command(self, text):
        return await self.bridge.execute(parse_bridge_command("$bridge wechat " + text), self.app, self.message)

    async def test_topic_help_uses_live_catalogue_with_pagination(self):
        feature = BridgeFeature()
        feature.register('custom', ExampleBridge)
        self.app.bridge_feature = feature
        help_feature = HelpFeature()
        pages = help_feature._paginate(feature.help_text())
        output = []
        for page in range(1, len(pages) + 1):
            self.message.content = f'$help bridge {page}'
            output.extend(await help_feature.handle(self.app, self.message))
        rendered = '\n'.join(response.text for response in output)
        self.assertIn('example, wechat, custom', rendered)
        self.assertIn('--whitelist defaults', rendered)
        self.assertTrue(all(len(response.text) < 2000 for response in output))

    async def test_native_reply_uses_recorded_origin_over_bound_chat_without_enabling_ai(self):
        await self.command('--whitelist add --chat Alice --kind direct')
        await self.command('--forward on --chat Alice')
        await self.command('--notify on --chat all')
        self.bridge._active = True
        self.app.platform.send_channel_message.return_value = SimpleNamespace(
            id='notice-123', channel=SimpleNamespace(id=self.message.channel_id))
        await self.bridge._notify({'account_id':'account', 'chat_id':'bob', 'type':'incoming',
                                  'message':{'text':'Bob message', 'outgoing':False}})
        feature = BridgeFeature({'WECHAT_BRIDGE_OWNER_ID':'owner'})
        feature._wechat = self.bridge
        # Reply routing must work even when another bridge is selected.
        feature._select(self.message.channel_id, 'example')
        self.message.content = 'Reply to Bob'
        self.message.metadata = {'referenced_message_id':'notice-123'}
        self.assertTrue(feature.wants_passive(self.message))
        await feature.handle_passive(self.app, self.message)
        self.transport.send.assert_awaited_once_with('bob', 'Reply to Bob')
        self.assertFalse(self.bridge.store.get('account', 'bob')['whitelisted'])
        self.assertFalse(self.bridge.store.get('account', 'bob')['agent_enabled'])
        self.transport.send.reset_mock()
        self.message.author_id = 'other'
        self.assertFalse(feature.wants_passive(self.message))
        await feature.handle_passive(self.app, self.message)
        self.transport.send.assert_not_awaited()

    async def test_untrusted_notification_reference_cannot_redirect_bound_relay(self):
        await self.command('--whitelist add --chat Alice --kind direct')
        await self.command('--forward on --chat Alice')
        self.bridge._active = True
        self.message.content = 'WeChat · Bob · incoming\nA copied notification'
        self.message.metadata = {'referenced_message_id':'forged'}
        with self.assertRaises(ValueError):
            await self.bridge.handle_passive(self.app, self.message)
        self.transport.send.assert_not_awaited()

    async def test_notification_routes_are_channel_and_account_scoped(self):
        await self.command('--notify on --chat all')
        self.bridge._active = True
        self.app.platform.send_channel_message.return_value = SimpleNamespace(
            id='notice-123', channel=SimpleNamespace(id=self.message.channel_id))
        await self.bridge._notify({'account_id':'account', 'chat_id':'bob', 'type':'incoming',
                                  'message':{'text':'message'}})
        self.message.content = 'reply'
        self.message.metadata = {'referenced_message_id':'notice-123'}
        self.message.channel_id = 'other-channel'
        self.assertIsNone(self.bridge.notification_route(self.message))
        self.message.channel_id = 'discord-test'
        self.transport.status.return_value = {'state':'ready', 'account_id':'different-account'}
        with self.assertRaises(PermissionError):
            await self.bridge.handle_passive(self.app, self.message)
        self.transport.send.assert_not_awaited()

    async def test_owner_gate_precedes_account_reads_and_qr_capture(self):
        self.message.author_id = "other"
        with self.assertRaises(PermissionError):
            await self.command("--login")
        self.transport.login.assert_not_awaited()
        self.transport.status.assert_not_awaited()

    async def test_qr_is_an_attachment_and_login_poll_can_confirm(self):
        result = await self.command("--login")
        self.assertEqual(result[0].attachment.path, "/tmp/qr.png")
        self.assertTrue(result[0].attachment.delete_after_send)
        self.transport.status.return_value = {"state": "confirming", "account_id": ""}
        self.transport.confirm_login.return_value = {"state": "logged_out", "account_id": ""}
        await self.bridge._poll_once()
        self.transport.confirm_login.assert_awaited_once()

    async def test_manual_send_requires_receipt_before_success_or_history(self):
        self.transport.send.return_value = {"verified": False}
        result = await self.command('--send "Hello" --to Alice')
        self.assertEqual(result[0].metadata["tool_status"], "failed")
        self.assertEqual(self.bridge.store.history("account", "alice"), [])
        self.transport.send.return_value = {"verified": True, "message_id": "sent-1"}
        result = await self.command('--send "Hello" --to Alice')
        self.assertNotIn("tool_status", result[0].metadata)
        self.assertTrue(self.bridge.store.history("account", "alice")[0]["outgoing"])

    async def test_automatic_reply_settings_baseline_existing_messages(self):
        old = {"id": "old", "chat_id": "alice", "sender_name": "Alice", "text": "old", "outgoing": False}
        self.transport.read_messages.return_value = [old]
        await self.command('--whitelist add --chat Alice --kind direct')
        await self.command('--agent on --chat Alice --delay 15 --instructions "Brief."')
        settings = self.bridge.store.get("account", "alice")
        self.assertTrue(settings["agent_enabled"])
        self.assertEqual(settings["delay_seconds"], 15)
        await self.bridge.supervisor.observe("account", "alice", [old])
        self.transport.send.assert_not_awaited()
        self.assertFalse(self.bridge.supervisor.status()["active_chats"])

    async def test_agent_enable_requires_whitelist_and_removal_revokes_work(self):
        with self.assertRaises(ValueError): await self.command('--agent on --chat Alice')
        await self.command('--whitelist add --chat Alice --kind direct')
        await self.command('--agent on --chat Alice')
        self.assertTrue(self.bridge.store.reply_ready('account','alice'))
        await self.command('--whitelist remove --chat Alice')
        self.assertFalse(self.bridge.store.reply_ready('account','alice'))
        self.assertFalse(self.bridge.store.get('account','alice')['agent_enabled'])

    async def test_live_whitelist_lists_groups_and_exact_aliases(self):
        await self.command('--whitelist defaults --mention "🎾🎾🎾🎾"')
        await self.command('--whitelist add --chat Alice --kind group --mention "Group Nick"')
        result=await self.command('--whitelist list')
        self.assertIn('Group Nick',result[0].text)
        self.assertIn('group',result[0].text)
        self.assertEqual(self.bridge.store.mention_names('account','alice'),['Group Nick'])

    async def test_owner_dm_notifications_are_independent_of_whitelist(self):
        self.app.platform.send_direct_message=AsyncMock()
        await self.command('--notify on --chat all --destination dm')
        self.assertFalse(self.bridge.store.get('account','alice')['whitelisted'])
        await self.bridge._notify({'account_id':'account','chat_id':'alice','type':'incoming',
                                  'message':{'text':'notification only','outgoing':False}})
        self.app.platform.send_direct_message.assert_awaited_once()
        self.assertEqual(self.app.platform.send_direct_message.await_args.args[0],'owner')
        self.transport.send.assert_not_awaited()

    async def test_bound_forwarding_survives_registry_recreation_and_revocation_blocks_it(self):
        await self.command('--whitelist add --chat Alice --kind direct')
        await self.command('--forward on --chat Alice')
        self.bridge._active=True
        feature=BridgeFeature({'WECHAT_BRIDGE_OWNER_ID':'owner'})
        feature.register('wechat',lambda channel:self.bridge)
        feature._wechat=self.bridge
        feature._select(self.message.channel_id,'wechat')
        self.message.content='a manual relay'
        result=await feature.handle_passive(self.app,self.message)
        self.assertIn('Forwarded',result[0].text)
        self.transport.send.assert_awaited_once_with('alice','a manual relay')
        await self.command('--whitelist remove --chat Alice')
        result=await feature.handle_passive(self.app,self.message)
        self.assertEqual(result[0].metadata['tool_status'],'failed')
        self.assertEqual(self.transport.send.await_count,1)

    async def test_notification_inheritance_optout_and_mentions(self):
        await self.command("--notify on --chat all")
        self.assertEqual(self.bridge._destination("account", "alice"), self.message.channel_id)
        await self.command("--notify off --chat Alice")
        self.assertIsNone(self.bridge._destination("account", "alice"))
        await self.command("--notify on --chat Alice")
        await self.bridge._notify({"account_id": "account", "chat_id": "alice", "type": "incoming",
                                  "message": {"sender_name": "Alice", "text": "@everyone hello"}})
        sent = self.app.platform.send_channel_message.await_args.args[1]
        self.assertNotIn("@everyone", sent)
        self.assertIn("hello", sent)

    async def test_first_poll_keeps_command_baseline_and_notifies_new_arrival(self):
        old = {"id": "old", "chat_id": "alice", "text": "old", "outgoing": False, "kind": "text"}
        new = {"id": "new", "chat_id": "alice", "text": "new arrival", "outgoing": False, "kind": "text"}
        self.transport.read_messages.return_value = [old]
        await self.command('--notify on --chat Alice')
        self.transport.read_messages.return_value = [old, new]
        await self.bridge._poll_once()
        sent = [call.args[1] for call in self.app.platform.send_channel_message.await_args_list]
        self.assertTrue(any("new arrival" in text for text in sent))
        self.assertFalse(any("\nParticipant: old" in text for text in sent))

    async def test_poll_skips_a_chat_waiting_for_file_receipt_instead_of_blocking_others(self):
        old = {"id": "old", "chat_id": "alice", "text": "old", "outgoing": False, "kind": "text"}
        self.transport.read_messages.return_value = [old]
        await self.command('--notify on --chat Alice')
        self.transport.read_messages.reset_mock()
        self.transport.chat_busy = lambda chat_id: True
        await self.bridge._poll_once()
        self.transport.read_messages.assert_not_awaited()
        self.transport.chat_busy = lambda chat_id: False
        await self.bridge._poll_once()
        self.transport.read_messages.assert_awaited_once()

    async def test_account_change_resets_baseline_and_unready_account_does_not_send(self):
        self.bridge._known_account = "old-account"
        self.transport.status.return_value = {"state": "logged_out", "account_id": "", "name": ""}
        await self.bridge._poll_once()
        self.assertIsNone(self.bridge._known_account)
        self.transport.read_messages.assert_not_awaited()
        self.transport.send.assert_not_awaited()

    async def test_registry_persists_selection_and_blocks_nonowner(self):
        feature = BridgeFeature({"WECHAT_BRIDGE_OWNER_ID": "owner"})
        feature.register("wechat", lambda channel: self.bridge)
        self.message.content = "$bridge wechat --status"
        await feature.handle(self.app, self.message)
        recreated = BridgeFeature({"WECHAT_BRIDGE_OWNER_ID": "owner"})
        self.assertEqual(recreated._selected(self.message.channel_id), "wechat")
        self.message.author_id = "other"
        self.transport.status.reset_mock()
        result = await feature.handle(self.app, self.message)
        self.assertEqual(result[0].metadata["tool_status"], "failed")
        self.transport.status.assert_not_awaited()

    async def test_example_runs_without_external_application(self):
        feature = BridgeFeature()
        self.message.content = "$bridge example --init"
        await feature.handle(self.app, self.message)
        self.message.content = '$bridge --send "你好"'
        result = await feature.handle(self.app, self.message)
        self.assertIn("你好", result[0].text)
        self.message.content = "$bridge --listen on"
        await feature.handle(self.app, self.message)
        self.message.content = "another message"
        result = await feature.handle_passive(self.app, self.message)
        self.assertIn("another message", result[0].text)
