from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from features.feature_agent import AgentFeature
from parsers.bridge_parser import parse_bridge_command


class BridgeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_does_not_advertise_unavailable_attachment_history(self):
        from services.wechat_agent import ConversationRegistry
        stub = SimpleNamespace(handle=AsyncMock())
        feature = AgentFeature(stub, stub, stub, stub)
        registry = ConversationRegistry(feature.registry, {'text', 'files'})
        self.assertNotIn('read_attachment', [tool['name'] for tool in registry.schemas()])
        with self.assertRaises(ValueError):
            registry.get('read_attachment')
        # The underlying tool remains available to the platform with durable attachments.
        self.assertEqual(feature.registry.get('read_attachment').name, 'read_attachment')

    async def test_agent_bridge_send_preserves_unicode_and_exact_recipient(self):
        stub = SimpleNamespace(handle=AsyncMock())
        bridge = SimpleNamespace(handle=AsyncMock(return_value=[]))
        feature = AgentFeature(stub, stub, stub, stub, bridge_feature=bridge)
        message = SimpleNamespace()
        # dataclasses.replace is intentionally used by the handlers.
        from app.bot_types import IncomingMessage
        from datetime import datetime, timezone
        message = IncomingMessage("", "channel", "owner", "Owner", "Owner", datetime.now(timezone.utc))
        await feature._bridge_control(None, message, {"action": "send", "bridge_type": "wechat",
            "message": '你好，"Alice"！', "target": "Alice Zhang"})
        value = parse_bridge_command(bridge.handle.await_args.args[1].content)
        self.assertFalse(value.errors)
        self.assertEqual(value.message, '你好，"Alice"！')
        self.assertEqual(value.target, "Alice Zhang")
        self.assertEqual(value.bridge_type, "wechat")
        self.assertEqual(feature.registry.get("bridge_read").risk, "read_only")
        self.assertEqual(feature.registry.get("bridge_control").risk, "write")
