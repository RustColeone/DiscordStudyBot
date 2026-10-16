"""A per-chat conversation uses the existing agent loop and shared tool registry."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from collections import defaultdict
from uuid import uuid4

from app.bot_types import BotResponse, IncomingMessage
from services.agent_loop import AgentLoop, AgentRun, signature
from services.agent_task import AgentTask
from services.command_registry import CommandRegistry, CommandTool
from services.tool_outcome import normalize_outcome


class ConversationChanged(asyncio.CancelledError):
    """The worker must incorporate fresh input before speaking or acting again."""


class ConversationRegistry(CommandRegistry):
    def __init__(self, source, capabilities):
        super().__init__()
        self.source, self.capabilities = source, set(capabilities)

    def get(self, name):
        tool = self._tools[name] if name in self._tools else self.source.get(name)
        if name.startswith('consult_') or not set(tool.required_capabilities).issubset(self.capabilities):
            raise ValueError("This conversation does not support that capability")
        return replace(tool, risk='write') if tool.owner_required else tool

    def schemas(self):
        available = []
        for tool in [*self.source.schemas(), *[t.schema() for t in self._tools.values()]]:
            try:
                available.append(self.get(tool['name']).schema())
            except ValueError:
                continue
        return available


class WeChatToolPlatform:
    supports_live_responses = True
    supports_message_edit = False
    supports_voice = False
    supports_markdown = False
    platform_name = 'wechat'

    def __init__(self, channel_id, publish):
        self.channel_id, self.publish = channel_id, publish

    async def send_channel_message(self, channel_id, text):
        return await self.send_channel_response(channel_id, BotResponse(text=text))

    async def send_channel_response(self, channel_id, response):
        if channel_id != self.channel_id:
            raise ValueError("A conversation tool cannot send to another chat")
        await self.publish(response)

    async def send_direct_message(self, *args):
        raise ValueError("A conversation tool cannot contact another person")

    def create_background_task(self, coroutine):
        coroutine.close()
        raise ValueError("Detached work needs an account service, not a conversation tool")


class WeChatConversationAgent:
    def __init__(self, bridge, app, generator):
        self.bridge, self.app, self.generator = bridge, app, generator
        self.registry = ConversationRegistry(app.agent_feature.registry, {'text', 'files'})
        self.sessions = {}
        self.scopes = {}
        self.confirmations = set()
        self.artifacts = defaultdict(dict)
        self.registry.register(CommandTool('deliver_prepared_file',
            'Deliver a retained file using its artifact ID to this conversation; do not redownload a prepared file.',
            {'artifact_id': {'type': 'string'}}, ['artifact_id'], 'write', self._retained_file,
            required_capabilities=('files',), approval_scope='conversation_delivery'))

    async def _retained_file(self, app, message, arguments):
        attachment = self.artifacts[message.channel_id].get(arguments['artifact_id'])
        if attachment is None or not Path(attachment.path).is_file():
            raise ValueError('That file is not retained for this conversation')
        return [BotResponse(attachment=attachment)]

    def file_inventory(self, key):
        result = []
        for identifier, attachment in self.artifacts[self.channel_id(key)].items():
            path = Path(attachment.path)
            if path.is_file():
                result.append({'artifact_id': identifier, 'filename': attachment.filename or path.name,
                               'size_bytes': path.stat().st_size})
        return result

    @staticmethod
    def channel_id(key):
        return 'wechat:' + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]

    def reset(self, key):
        self.sessions.pop(key, None)
        self.scopes.pop(key, None)
        self.confirmations.discard(key)

    def pending(self, key):
        state = self.sessions.get(key)
        if state is None:
            return None
        return {'task_id': state.task.task_id, 'status': state.status,
                'actions': state.pending_actions, 'steps': state.steps}

    def approve(self, key, task_id):
        state = self.sessions.get(key)
        settings = self.bridge.store.get(*key)
        if (state is None or state.status != 'awaiting_confirmation' or state.task.task_id != task_id
                or not settings['agent_enabled'] or not settings.get('whitelisted') or self.scopes.get(key, (None,))[0] != settings['revision']):
            raise ValueError("That exact pending task is no longer available. Check --agent status.")
        self.confirmations.add(key)

    async def run(self, context, publish, valid):
        key = (context['account_id'], context['chat_id'])
        scope = (context['settings']['revision'], context['epoch'])
        if context.get('reply_target'):
            scope += (context.get('reply_request_id') or context['reply_target']['id'],)
        if self.scopes.get(key) != scope:
            self.reset(key)
        self.scopes[key] = scope
        state = self.sessions.get(key)
        request = '\n'.join(str(m.get('text') or '') for m in context['messages'])
        if state is None or state.status in {'complete', 'blocked', 'cancelled'}:
            if not request.strip():
                return
            state = AgentRun(AgentTask(goal=request, participants=['DeepSeek']))
            self.sessions[key] = state
            answer = ''
        else:
            answer = request
            # A new user turn gets a fresh bounded allowance while retaining observations.
            state.steps = state.actions_taken = 0
        channel = self.channel_id(key)
        platform = WeChatToolPlatform(channel, publish)
        scoped_app = SimpleNamespace(config=self.app.config, platform=platform)
        target = context.get('reply_target') or {}
        original = IncomingMessage(request, channel, target.get('sender_id') or channel + ':participant',
                                   target.get('sender_name') or 'Participant', target.get('sender_name') or 'Participant',
                                   datetime.now(timezone.utc), is_dm=True,
                                   metadata={'platform': 'wechat', 'upload_limit_mb': self.bridge.upload_limit_mb,
                                             'upload_limit_source': 'bridge_policy'})
        context = {**context, 'runtime': self.app.agent_feature._runtime_context(self.app, original),
                   'delivery_policy': {'max_file_mb': self.bridge.upload_limit_mb, 'source': 'bridge_policy',
                                       'platform_limit_mb': None},
                   'prepared_files': self.file_inventory(key)}

        async def generate(work, user_answer, correction):
            if not valid():
                raise ConversationChanged()
            return await self.generator.step(context, work, self.registry.schemas(), user_answer, correction)

        async def speak(text):
            if not valid():
                raise ConversationChanged()
            await publish(BotResponse(text=text))

        async def progress(text):
            await self.bridge.work_notice(key, 'progress', text)

        async def approval(actions):
            await self.bridge.work_notice(key, 'approval',
                "Owner approval required for these exact actions:\n" + json.dumps(actions, ensure_ascii=False)
                + f'\n$bridge wechat --approve --chat {json.dumps(key[1], ensure_ascii=False)} --task {state.task.task_id}')

        def validate(actions):
            if len(actions) > 5:
                raise ValueError("Use at most five actions in one step")
            for action in actions:
                if not isinstance(action, dict) or set(action) != {'tool', 'arguments'}:
                    raise ValueError("Actions require exactly tool and arguments")
                self.registry.validate_arguments(self.registry.get(action['tool']), action['arguments'])

        async def execute(action):
            if not valid():
                raise ConversationChanged()
            tool = self.registry.get(action['tool'])
            message = replace(original, metadata={**original.metadata, 'tool_progress': progress})
            if tool.owner_required:
                # AgentLoop reaches this only after the exact action was approved by the Discord owner.
                owner = str(self.app.config.get('WECHAT_BRIDGE_OWNER_ID') or self.app.config.get('CREATOR_USER_ID') or '')
                message = replace(message, author_id=owner,
                                  metadata={**message.metadata, 'approved_external_request': original.author_id})
            running = asyncio.create_task(self.registry.execute(action['tool'], scoped_app, message, action['arguments']))
            try:
                while not running.done():
                    await asyncio.wait({running}, timeout=15)
                    if not running.done():
                        await progress(f"{action['tool']} is still running.")
                outcome = normalize_outcome(running.result())
                if outcome.status == 'success' and tool.risk == 'prepare':
                    state.writes_completed.add(signature(action))
                for response in outcome.responses:
                    if response.attachment is None or outcome.status != 'success':
                        continue
                    artifact_id = uuid4().hex
                    retained = self.artifacts[channel]
                    retained[artifact_id] = response.attachment
                    while len(retained) > 5:
                        retained.pop(next(iter(retained)))
                    outcome.summary += f'\nRetained artifact ID: {artifact_id}'
                    try:
                        await publish(BotResponse(attachment=response.attachment))
                    except ConversationChanged:
                        # Keep the tool outcome/file so fresh input does not re-run a completed download.
                        state.observations.append({'action': action, **outcome.context()})
                        raise
                    except Exception:
                        outcome.status, outcome.error_code, outcome.retryable = 'failed', 'delivery_failed', False
                        outcome.summary = 'File delivery could not be verified. Do not retry it automatically.\n' + outcome.summary
                        break
                    outcome.summary += '\nThe desktop shows this file in the current conversation.'
                    retained.pop(artifact_id, None)
                    if response.attachment.delete_after_send:
                        Path(response.attachment.path).unlink(missing_ok=True)
                return outcome
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

        confirm = key in self.confirmations
        self.confirmations.discard(key)
        def preauthorized(tool, action):
            settings = self.bridge.store.get(*key)
            return (valid() and settings.get('whitelisted') and settings.get('agent_enabled')
                    and settings.get('revision') == scope[0] and not tool.owner_required
                    and tool.approval_scope == 'conversation_delivery')
        loop = AgentLoop(self.registry, generate, execute, speak, progress, approval, validate,
                         max_steps=12, max_actions=24, preauthorized=preauthorized)
        await loop.run(state, answer, confirm)
        if state.status in {'complete', 'blocked'}:
            self.reset(key)
