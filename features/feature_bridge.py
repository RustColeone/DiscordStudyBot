"""Named bridge registry and command routing, independent of an external account transport."""
import asyncio
from typing import Dict

from app.bot_types import BotResponse, IncomingMessage
from bridges.example_bridge import ExampleBridge
from parsers.bridge_parser import parse_bridge_command
from parsers.bridge_commands import bridge_help
from services import database as db


BRIDGE_HELP = bridge_help()


class BridgeFeature:
    def __init__(self, config=None):
        self.config = config or {}
        self.instances: Dict[tuple, object] = {}
        self.factories = {}
        self.available = True
        self._app = None
        self._wechat = None
        self.register("example", ExampleBridge)
        self.register("wechat", self._wechat_factory)
        self.parse_bridge_command = parse_bridge_command

    def register(self, name, factory):
        if not name or not callable(factory):
            raise ValueError("A bridge needs a name and callable factory")
        self.factories[name] = factory

    def _wechat_factory(self, channel_id):
        if self._wechat is None:
            from bridges.wechat_bridge import WeChatBridge
            self._wechat = WeChatBridge(self.config)
            if self._app is not None:
                self._wechat.attach(self._app)
        return self._wechat

    @staticmethod
    def _bindings():
        with db.get_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS bridge_bindings (
                channel_id TEXT PRIMARY KEY, bridge_type TEXT NOT NULL)""")

    def _selected(self, channel_id):
        self._bindings()
        with db.get_db() as conn:
            row = conn.execute("SELECT bridge_type FROM bridge_bindings WHERE channel_id=?", (str(channel_id),)).fetchone()
        return row[0] if row else self.config.get("BRIDGE_DEFAULT", "example")

    def _select(self, channel_id, name):
        self._bindings()
        with db.get_db() as conn:
            conn.execute("INSERT INTO bridge_bindings VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET bridge_type=excluded.bridge_type",
                         (str(channel_id), name))

    def can_handle(self, message: IncomingMessage):
        return message.content == "$bridge" or message.content.startswith("$bridge ")

    def _authorize(self, name, message):
        if name == "wechat":
            owner = str(self.config.get("WECHAT_BRIDGE_OWNER_ID") or self.config.get("CREATOR_USER_ID") or "").strip()
            if not owner:
                raise ValueError("Set WECHAT_BRIDGE_OWNER_ID (or CREATOR_USER_ID) before using the account bridge.")
            if str(message.author_id) != owner:
                raise PermissionError("WeChat account commands are restricted to the configured owner.")

    async def handle(self, app, message):
        command = self.parse_bridge_command(message.content)
        if command.errors:
            return [BotResponse(text="\n".join(command.errors), metadata={"tool_status": "failed"})]
        if command.action == "help":
            return [BotResponse(text=self.help_text())]
        name = command.bridge_type or self._selected(message.channel_id)
        try:
            if name not in self.factories:
                raise ValueError("Unknown bridge. Available: " + ", ".join(self.factories))
            self._authorize(name, message)
            key = (str(message.channel_id), name)
            bridge = self.instances.get(key)
            if bridge is None:
                bridge = self.factories[name](message.channel_id)
                self.instances[key] = bridge
            if hasattr(bridge, "attach"):
                bridge.attach(app)
            responses = await bridge.execute(command, app, message)
            if command.bridge_type:
                self._select(message.channel_id, name)
            if command.action == "disconnect" and name != "wechat":
                self.instances.pop(key, None)
            return responses
        except (ValueError, PermissionError, RuntimeError) as error:
            return [BotResponse(text=str(error), metadata={"tool_status": "failed", "retryable": False})]

    async def handle_passive(self, app, message):
        if message.content.startswith("$"):
            return []
        if self._wechat is not None and self._wechat.notification_route(message):
            try:
                self._authorize('wechat',message)
                return await self._wechat.handle_passive(app,message)
            except (ValueError,PermissionError,RuntimeError) as error:
                return [BotResponse(text=str(error),metadata={'tool_status':'failed'})]
        name = self._selected(message.channel_id)
        bridge = self.instances.get((str(message.channel_id), name))
        if bridge is None and name == 'wechat':
            bridge = self._wechat_factory(str(message.channel_id))
        if bridge is not None and hasattr(bridge, 'handle_passive'):
            if not bridge.wants_passive(message.channel_id):
                return []
            try:
                self._authorize(name, message)
                return await bridge.handle_passive(app, message)
            except (ValueError, PermissionError, RuntimeError) as error:
                return [BotResponse(text=str(error), metadata={'tool_status':'failed'})]
        if bridge is None or not bridge.is_listening():
            return []
        try:
            self._authorize(name, message)
            reply = await bridge.send_message(message.content)
            return [BotResponse(text=reply)] if reply else []
        except (ValueError, PermissionError, RuntimeError) as error:
            return [BotResponse(text=str(error), metadata={"tool_status": "failed"})]

    def help_text(self):
        return bridge_help(self.factories)

    def wants_passive(self,message):
        if self._wechat is not None and self._wechat.notification_route(message):
            try: self._authorize('wechat',message)
            except (ValueError,PermissionError): return False
            return True
        name=self._selected(message.channel_id)
        try:
            self._authorize(name,message)
        except (ValueError,PermissionError):
            return False
        bridge=self.instances.get((str(message.channel_id),name))
        if bridge is None and name=='wechat': bridge=self._wechat_factory(str(message.channel_id))
        if bridge is None: return False
        return bridge.wants_passive(message.channel_id) if hasattr(bridge,'wants_passive') else bridge.is_listening()

    async def start(self, app):
        self._app = app
        # Resume only persisted, explicitly configured subscriptions/reply settings.
        owner = self.config.get("WECHAT_BRIDGE_OWNER_ID") or self.config.get("CREATOR_USER_ID")
        if owner:
            bridge = self._wechat_factory("")
            await bridge.start_if_configured()

    async def close(self):
        seen = set()
        for bridge in [*self.instances.values(), self._wechat]:
            if bridge is None or id(bridge) in seen:
                continue
            seen.add(id(bridge))
            close = getattr(bridge, "close", None)
            if close is not None:
                await close()
            else:
                await bridge.disconnect()
