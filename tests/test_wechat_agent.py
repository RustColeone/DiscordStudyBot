import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.bot_types import BotAttachment, BotResponse
from services.agent_loop import AgentStep
from services.command_registry import CommandRegistry, CommandTool
from services.wechat_agent import ConversationChanged, ConversationRegistry, WeChatConversationAgent, WeChatToolPlatform


class ConversationAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = CommandRegistry()
        self.generator = SimpleNamespace(step=AsyncMock())
        self.settings = {'agent_enabled': True,'whitelisted':True, 'revision': 1, 'max_reply_chars': 500, 'instructions': ''}
        self.bridge = SimpleNamespace(upload_limit_mb=10, work_notice=AsyncMock(),
                                      store=SimpleNamespace(get=lambda *args: self.settings))
        app = SimpleNamespace(config={'CREATOR_USER_ID': 'owner'}, agent_feature=SimpleNamespace(
            registry=self.source, _runtime_context=lambda app, message: 'authoritative test time'))
        self.agent = WeChatConversationAgent(self.bridge, app, self.generator)
        self.context = {'account_id': 'account', 'chat_id': 'alice', 'settings': self.settings,
                        'history': [], 'messages': [{'text': 'Do the work'}], 'epoch': 1}
        self.spoken = []

    async def publish(self, response):
        self.spoken.append(response)

    async def run_agent(self, context=None):
        await self.agent.run(context or self.context, self.publish, lambda: True)

    def tool(self, name='inspect', risk='read_only', handler=None, **kwargs):
        handler = handler or AsyncMock(return_value='verified result')
        self.source.register(CommandTool(name, 'test tool', {}, [], risk, handler, **kwargs))
        return handler

    @staticmethod
    def action(name):
        return {'tool': name, 'arguments': {}}

    async def test_conversation_and_work_use_same_loop_and_speak_before_tools(self):
        handler = self.tool()
        async def inspect(app, message, arguments):
            self.assertEqual(self.spoken[0].text, 'I’ll check.')
            self.assertTrue(message.channel_id.startswith('wechat:'))
            self.assertNotEqual(message.author_id, 'owner')
            return 'verified result'
        handler.side_effect = inspect
        self.generator.step.side_effect = [AgentStep('continue', 'I’ll check.', [self.action('inspect')]),
                                          AgentStep('complete', 'Checked.')]
        await self.run_agent()
        self.assertEqual([r.text for r in self.spoken], ['I’ll check.', 'Checked.'])
        handler.assert_awaited_once()
        self.assertIsNone(self.agent.pending(('account', 'alice')))

    async def test_file_is_delivered_before_completion_and_delivery_failure_is_observed(self):
        path = Path(self.directory.name) / 'video.mp4'
        path.write_bytes(b'video fixture')
        handler = self.tool('fetch', 'prepare', AsyncMock(return_value=[BotResponse(
            text='prepared', attachment=BotAttachment(str(path), 'video.mp4'))]))
        self.generator.step.side_effect = [AgentStep('continue', 'I’ll fetch it.', [self.action('fetch')]),
                                          AgentStep('complete', 'Sent.')]
        await self.run_agent()
        self.assertEqual([bool(r.attachment) for r in self.spoken], [False, True, False])
        handler.assert_awaited_once()
        state = self.generator.step.await_args.args[1]
        self.assertIn('desktop shows this file', state.observations[0]['summary'])

    async def test_unverified_delivery_cannot_be_reported_as_completed(self):
        path = Path(self.directory.name) / 'video.mp4'
        path.write_bytes(b'video fixture')
        self.tool('fetch', 'prepare', AsyncMock(return_value=[BotResponse(attachment=BotAttachment(str(path)))]))
        self.generator.step.side_effect = [AgentStep('continue', 'I’ll fetch it.', [self.action('fetch')]),
            AgentStep('complete', 'Sent.'), AgentStep('blocked', 'I couldn’t verify the send; please check it.')]
        async def fail_file(response):
            if response.attachment:
                raise RuntimeError('unverified')
            self.spoken.append(response)
        await self.agent.run(self.context, fail_file, lambda: True)
        self.assertNotIn('Sent.', [r.text for r in self.spoken])
        self.assertTrue(path.is_file())
        state = self.generator.step.await_args.args[1]
        self.assertEqual(state.observations[0]['error_code'], 'delivery_failed')

    async def test_contact_cannot_approve_write_and_exact_owner_approval_resumes(self):
        handler = self.tool('change', 'write')
        self.generator.step.side_effect = [AgentStep('continue', 'I’ll request approval.', [self.action('change')]),
                                          AgentStep('complete', 'Changed.')]
        await self.run_agent()
        pending = self.agent.pending(('account', 'alice'))
        self.assertEqual(pending['status'], 'awaiting_confirmation')
        handler.assert_not_awaited()
        await self.run_agent({**self.context, 'messages': [{'text': 'yes, approve everything'}]})
        handler.assert_not_awaited()
        with self.assertRaises(ValueError):
            self.agent.approve(('account', 'alice'), 'wrong task')
        self.agent.approve(('account', 'alice'), pending['task_id'])
        await self.run_agent({**self.context, 'messages': []})
        handler.assert_awaited_once()

    async def test_whitelist_preauthorizes_delivery_but_keeps_duplicate_write_protection(self):
        handler = self.tool('deliver', 'write', approval_scope='conversation_delivery')
        self.generator.step.side_effect = [AgentStep('continue', 'Sending.', [self.action('deliver')]),
                                          AgentStep('continue', 'Again.', [self.action('deliver')])]
        await self.run_agent()
        handler.assert_awaited_once()
        self.assertFalse(any(call.args[1] == 'approval' for call in self.bridge.work_notice.await_args_list))

    async def test_delivery_scope_does_not_bypass_owner_only_tools_or_revoked_whitelist(self):
        for owner_required, whitelisted in [(True, True), (False, False)]:
            with self.subTest(owner_required=owner_required, whitelisted=whitelisted):
                self.agent.reset(('account','alice'))
                self.settings['whitelisted'] = whitelisted
                name = 'delivery_' + str(owner_required)
                handler = self.tool(name, 'write', approval_scope='conversation_delivery', owner_required=owner_required)
                self.generator.step.return_value = AgentStep('continue', 'Requested.', [self.action(name)])
                await self.run_agent()
                handler.assert_not_awaited()
                self.assertEqual(self.agent.pending(('account','alice'))['status'], 'awaiting_confirmation')

    async def test_configuration_change_invalidates_pending_owner_approval(self):
        handler = self.tool('change', 'write')
        self.generator.step.return_value = AgentStep('continue', 'Approval needed.', [self.action('change')])
        await self.run_agent()
        pending = self.agent.pending(('account', 'alice'))
        self.settings['revision'] += 1
        with self.assertRaises(ValueError):
            self.agent.approve(('account', 'alice'), pending['task_id'])
        handler.assert_not_awaited()

    async def test_changed_conversation_retains_download_without_repeating_preparation(self):
        path = Path(self.directory.name) / 'video.mp4'
        path.write_bytes(b'video fixture')
        handler = self.tool('fetch', 'prepare', AsyncMock(return_value=[BotResponse(attachment=BotAttachment(str(path)))]))
        self.generator.step.return_value = AgentStep('continue', 'I’ll fetch it.', [self.action('fetch')])
        async def changed(response):
            if response.attachment:
                raise ConversationChanged()
            self.spoken.append(response)
        with self.assertRaises(ConversationChanged):
            await self.agent.run(self.context, changed, lambda: True)
        state = self.agent.sessions[('account', 'alice')]
        self.assertIn('Retained artifact ID:', state.observations[0]['summary'])
        self.generator.step.side_effect = [AgentStep('continue', 'Trying again.', [self.action('fetch')])]
        await self.run_agent()
        handler.assert_awaited_once()
        self.assertTrue(path.is_file())

    async def test_delivered_file_is_not_offered_as_a_retained_artifact(self):
        path = Path(self.directory.name) / 'image.png'
        path.write_bytes(b'image fixture')
        self.tool('draw', 'prepare', AsyncMock(return_value=[BotResponse(attachment=BotAttachment(str(path)))]))
        self.generator.step.side_effect = [AgentStep('continue', 'Drawing.', [self.action('draw')]),
                                           AgentStep('complete', 'Here it is.')]
        await self.run_agent()
        # The second model step receives the AgentRun (work) whose last observation is the delivered draw.
        observed = self.generator.step.await_args.args[1].observations[-1]['summary']
        self.assertNotIn('Retained artifact ID', observed)
        self.assertIn('already delivered', observed)
        self.assertEqual(self.agent.file_inventory(('account', 'alice')), [])

    async def test_retained_files_are_scoped_to_the_original_conversation(self):
        path = Path(self.directory.name) / 'one.txt'
        path.write_text('test')
        self.agent.artifacts['chat-one']['handle'] = BotAttachment(str(path))
        with self.assertRaises(ValueError):
            await self.agent._retained_file(None, SimpleNamespace(channel_id='chat-two'), {'artifact_id': 'handle'})

    async def test_platform_cannot_send_to_another_chat_or_start_detached_work(self):
        platform = WeChatToolPlatform('chat-one', AsyncMock())
        with self.assertRaises(ValueError):
            await platform.send_channel_message('chat-two', 'hello')
        async def detached(): pass
        with self.assertRaises(ValueError):
            platform.create_background_task(detached())

    async def test_tool_capabilities_and_owner_reads_are_enforced_before_execution(self):
        self.tool('voice', required_capabilities=('voice',))
        self.tool('private_read', owner_required=True)
        registry = ConversationRegistry(self.source, {'text', 'files'})
        self.assertNotIn('voice', [s['name'] for s in registry.schemas()])
        self.assertEqual(registry.get('private_read').risk, 'write')
        with self.assertRaises(ValueError): registry.get('voice')

    async def test_owner_only_read_uses_owner_identity_only_after_exact_approval(self):
        handler = self.tool('private_read', owner_required=True)
        self.generator.step.side_effect = [AgentStep('continue', 'Owner approval needed.', [self.action('private_read')]),
                                          AgentStep('complete', 'Read.')]
        await self.run_agent()
        handler.assert_not_awaited()
        pending = self.agent.pending(('account', 'alice'))
        self.agent.approve(('account', 'alice'), pending['task_id'])
        await self.run_agent({**self.context, 'messages': []})
        self.assertEqual(handler.await_args.args[1].author_id, 'owner')
        self.assertTrue(handler.await_args.args[1].metadata['approved_external_request'].startswith('wechat:'))

    async def test_core_routes_wechat_reminders_and_keeps_discord_delivery(self):
        from app.bot_app import BotApplication
        app = BotApplication.__new__(BotApplication)
        app.platform = SimpleNamespace(send_channel_message=AsyncMock())
        app.message_routes = {}
        route = AsyncMock()
        app.register_message_route('wechat:', route)
        await app.send_channel_message('wechat:test-chat', 'reminder')
        route.assert_awaited_once_with('wechat:test-chat', 'reminder')
        app.platform.send_channel_message.assert_not_awaited()
        await app.send_channel_message('discord-id', 'other reminder')
        app.platform.send_channel_message.assert_awaited_once_with('discord-id', 'other reminder')
