import asyncio

from features.feature_bridge import BridgeFeature
from features.feature_agent import AgentFeature
from features.feature_admin import AdminFeature
from features.feature_broadcast import BroadcastFeature
from features.feature_chat import ChatFeature
from features.feature_clip import ClipFeature
from features.feature_database import DatabaseFeature
from features.feature_help import HelpFeature
from features.feature_music import MusicFeature
from features.feature_search import SearchFeature
from features.feature_system import SystemFeature
from features.feature_time import TimeFeature
from services.attachment_store import cleanup_expired_cache


class BotApplication:
    def __init__(self, config):
        self.config = config
        self.platform = None
        self.attachment_cleanup_task = None
        cleanup_expired_cache()

        self.help_feature = HelpFeature()
        self.admin_feature = AdminFeature()
        self.chat_feature = ChatFeature()
        self.time_feature = TimeFeature()
        self.system_feature = SystemFeature()
        self.clip_feature = ClipFeature()
        self.broadcast_feature = BroadcastFeature()
        self.music_feature = MusicFeature()
        self.bridge_feature = BridgeFeature()
        self.search_feature = SearchFeature()
        self.database_feature = DatabaseFeature()
        self.agent_feature = AgentFeature(
            self.time_feature,
            self.system_feature,
            self.search_feature,
            self.help_feature,
            self.music_feature,
            self.clip_feature,
            self.database_feature,
            self.bridge_feature,
            self.broadcast_feature,
        )

        self.command_features = [
            self.help_feature,
            self.admin_feature,
            self.agent_feature,
            self.time_feature,
            self.system_feature,
            self.clip_feature,
            self.broadcast_feature,
            self.music_feature,
            self.bridge_feature,
            self.search_feature,
            self.chat_feature,
            self.database_feature,
        ]

    def attach_platform(self, platform) -> None:
        self.platform = platform

    def start_background_tasks(self) -> None:
        self.time_feature.start_scheduler(self)
        if self.attachment_cleanup_task is None or self.attachment_cleanup_task.done():
            self.attachment_cleanup_task = self.platform.create_background_task(
                self._run_attachment_cleanup()
            )

    @staticmethod
    async def _run_attachment_cleanup() -> None:
        while True:
            await asyncio.sleep(5 * 60)
            await asyncio.to_thread(cleanup_expired_cache)

    async def handle_message(self, message):
        self._log_message(message)
        if message.author_is_bot:
            return []

        if not message.content.startswith("$"):
            responses = await self.chat_feature.handle_passive(self, message)
            if responses:
                return responses

            responses = await self.bridge_feature.handle_passive(self, message)
            if responses:
                return responses
            return []

        for feature in self.command_features:
            if feature.can_handle(message):
                return await feature.handle(self, message)

        return []

    def _log_message(self, message) -> None:
        msg_channel = "Private/"
        if message.guild_name:
            msg_channel = message.guild_name
        if message.is_dm:
            msg_channel += "/DM"
        elif message.channel_name:
            msg_channel += f"/{message.channel_name}"
        print(f'From {msg_channel}, by {message.author_name}: "{message.content}"')
