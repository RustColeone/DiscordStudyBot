import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from adapters.discord_adapter import DiscordAdapter
from app.bot_types import BotAttachment, BotResponse


class DiscordAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_turns_are_sent_as_separate_ordered_messages(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        channel = SimpleNamespace(send=AsyncMock())
        responses = [
            BotResponse(text="**DeepSeek**\nFirst"),
            BotResponse(text="**Luna**\nSecond"),
            BotResponse(text="**Gemini**\nThird"),
            BotResponse(text="**Claude**\nFourth"),
        ]

        await adapter._send_responses(channel, responses)

        self.assertEqual(
            [call.args[0] for call in channel.send.await_args_list],
            ["**DeepSeek**\nFirst", "**Luna**\nSecond", "**Gemini**\nThird", "**Claude**\nFourth"],
        )

    async def test_live_room_turn_is_not_sent_twice(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        channel = SimpleNamespace(send=AsyncMock())

        await adapter._send_responses(channel, [
            BotResponse(text="**DeepSeek**\nAlready sent", metadata={"sent_live": True}),
            BotResponse(text="**Sources**\nSource"),
        ])

        channel.send.assert_awaited_once_with("**Sources**\nSource", suppress_embeds=False)

    def setUp(self):
        self.bot_user = SimpleNamespace(id=42)
        self.adapter = DiscordAdapter.__new__(DiscordAdapter)
        self.adapter.allowed_channel_ids = {"100"}
        self.adapter.require_mention = True
        self.adapter.creator_user_id = ""
        self.adapter.creator_only_mode = False
        self.adapter.client = SimpleNamespace(user=self.bot_user)

    def message(self, channel_id="100", mentions=None, author_is_bot=False):
        return SimpleNamespace(
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(bot=author_is_bot),
            mentions=mentions or [],
        )

    def test_allows_mention_in_configured_channel(self):
        self.assertTrue(self.adapter._is_allowed_message(self.message(mentions=[self.bot_user])))

    def test_rejects_other_channels_and_unmentioned_messages(self):
        self.assertFalse(self.adapter._is_allowed_message(self.message(channel_id="200", mentions=[self.bot_user])))
        self.assertFalse(self.adapter._is_allowed_message(self.message()))

    def test_rejects_bot_messages(self):
        self.assertFalse(
            self.adapter._is_allowed_message(self.message(mentions=[self.bot_user], author_is_bot=True))
        )

    def test_parses_channel_list_and_boolean(self):
        config = {"DISCORD_ALLOWED_CHANNELS": "100, 200", "ID_CHANNEL": "300"}
        self.assertEqual(DiscordAdapter._parse_allowed_channel_ids(config), {"100", "200"})
        self.assertTrue(DiscordAdapter._parse_bool("yes"))
        self.assertFalse(DiscordAdapter._parse_bool("false"))

    def test_legacy_channel_setting_does_not_restrict_commands(self):
        self.assertEqual(DiscordAdapter._parse_allowed_channel_ids({"ID_CHANNEL": "300"}), set())

    def test_creator_only_mode_rejects_other_users(self):
        self.adapter.creator_user_id = "7"
        self.adapter.creator_only_mode = True

        creator = self.message(mentions=[self.bot_user])
        creator.author.id = 7
        other_user = self.message(mentions=[self.bot_user])
        other_user.author.id = 8

        self.assertTrue(self.adapter._is_allowed_message(creator))
        self.assertFalse(self.adapter._is_allowed_message(other_user))

    def test_mention_gate_defaults_to_disabled(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.allowed_channel_ids = {"100"}
        adapter.require_mention = DiscordAdapter._parse_bool({}.get("DISCORD_REQUIRE_MENTION", False))
        adapter.creator_user_id = ""
        adapter.creator_only_mode = False
        adapter.client = SimpleNamespace(user=self.bot_user)

        self.assertTrue(adapter._is_allowed_message(self.message()))

    def test_mentioned_natural_language_becomes_agent_request(self):
        native_message = SimpleNamespace(
            content="<@42> remind me tomorrow at this time to test",
            guild=None,
            channel=SimpleNamespace(id="100", name="general"),
            author=SimpleNamespace(id=7, name="User", display_name="User", bot=False),
            created_at=None,
            mentions=[self.bot_user],
        )

        incoming = self.adapter._to_incoming_message(native_message)

        self.assertEqual(incoming.content, "$agent remind me tomorrow at this time to test")
        self.assertTrue(incoming.metadata["was_mentioned"])

    def test_captures_replied_message_and_attachment_context(self):
        referenced = SimpleNamespace(
            content="https://youtu.be/music",
            attachments=[SimpleNamespace(url="https://cdn.example/video.mp4")],
        )
        native_message = SimpleNamespace(
            content="add that music",
            guild=None,
            channel=SimpleNamespace(id="100", name="general"),
            author=SimpleNamespace(id=7, name="User", display_name="User", bot=False),
            created_at=None,
            mentions=[],
            attachments=[],
            reference=SimpleNamespace(resolved=referenced),
        )

        incoming = self.adapter._to_incoming_message(native_message)

        self.assertEqual(incoming.metadata["referenced_content"], "https://youtu.be/music")
        self.assertEqual(
            incoming.metadata["referenced_attachment_urls"],
            ["https://cdn.example/video.mp4"],
        )

    def test_captures_only_supported_image_attachments_for_vision(self):
        native_message = SimpleNamespace(
            content="what is this?",
            guild=None,
            channel=SimpleNamespace(id="100", name="general"),
            author=SimpleNamespace(id=7, name="User", display_name="User", bot=False),
            created_at=None,
            mentions=[],
            attachments=[
                SimpleNamespace(
                    url="https://cdn.discordapp.com/photo.png",
                    filename="photo.png",
                    content_type="image/png",
                    size=1024,
                ),
                SimpleNamespace(
                    url="https://cdn.discordapp.com/notes.txt",
                    filename="notes.txt",
                    content_type="text/plain",
                    size=100,
                ),
            ],
            reference=None,
        )

        incoming = self.adapter._to_incoming_message(native_message)

        self.assertEqual(
            incoming.metadata["image_attachments"],
            [{
                "url": "https://cdn.discordapp.com/photo.png",
                "filename": "photo.png",
                "content_type": "image/png",
                "size": 1024,
            }],
        )

    def test_splits_long_responses_at_discord_limit(self):
        content = "x" * 4500

        chunks = self.adapter._split_message(content)

        self.assertEqual([len(chunk) for chunk in chunks], [2000, 2000, 500])
        self.assertEqual("".join(chunks), content)

    async def test_source_response_suppresses_discord_embeds(self):
        channel = SimpleNamespace(send=AsyncMock())

        await self.adapter._send_responses(
            channel,
            [BotResponse(text="Answer\nhttps://example.com", metadata={"suppress_embeds": True})],
        )

        channel.send.assert_awaited_once_with(
            "Answer\nhttps://example.com",
            suppress_embeds=True,
        )

    async def test_successful_attachment_upload_deletes_temporary_file(self):
        descriptor, path = tempfile.mkstemp(suffix=".mp4")
        os.close(descriptor)
        channel = SimpleNamespace(send=AsyncMock())
        response = BotResponse(
            text="clip ready",
            attachment=BotAttachment(path=path, filename="clip.mp4", delete_after_send=True),
        )

        with patch("adapters.discord_adapter.discord.File", return_value=object()):
            await self.adapter._send_attachment_response(channel, response)

        self.assertFalse(os.path.exists(path))

    async def test_failed_attachment_upload_retains_rendered_file(self):
        descriptor, path = tempfile.mkstemp(suffix=".mp4")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        channel = SimpleNamespace(send=AsyncMock(side_effect=[
            RuntimeError("TLS failure"),
            RuntimeError("TLS failure"),
            RuntimeError("TLS failure"),
            None,
        ]))
        response = BotResponse(
            text="clip ready",
            attachment=BotAttachment(path=path, filename="clip.mp4", delete_after_send=True),
        )

        with patch("adapters.discord_adapter.discord.File", return_value=object()), \
                patch("adapters.discord_adapter.asyncio.sleep", new=AsyncMock()):
            await self.adapter._send_attachment_response(channel, response)

        self.assertTrue(os.path.exists(path))
        fallback = channel.send.await_args_list[-1].args[0]
        self.assertIn("Attachment upload failed after 3 attempts", fallback)
        self.assertIn("retained on the bot host", fallback)


if __name__ == "__main__":
    unittest.main()