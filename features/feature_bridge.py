from typing import Dict

from app.bot_types import BotResponse, HandlerResult, IncomingMessage


class _FallbackBridgeCommand:
    def __init__(self):
        self.action = None
        self.message = None
        self.toggle_listen = False
        self.listen_mode = None
        self.errors = []
        self.show_status = False


def _fallback_parse_bridge_command(command_text: str):
    cmd = _FallbackBridgeCommand()
    text = command_text.strip()
    if text.startswith("$bridge"):
        text = text[7:].strip()

    if not text:
        cmd.show_status = True
        return cmd

    tokens = text.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ["--listen", "-l"]:
            if index + 1 < len(tokens) and tokens[index + 1].lower() in ["on", "off"]:
                cmd.toggle_listen = True
                cmd.listen_mode = tokens[index + 1].lower()
                index += 2
            else:
                cmd.toggle_listen = True
                index += 1
        elif token in ["--status", "-st"]:
            cmd.show_status = True
            index += 1
        elif token in ["--send", "-s"]:
            cmd.action = "send"
            cmd.message = " ".join(tokens[index + 1 :])
            break
        elif token in ["--init", "-i"]:
            cmd.action = "init"
            index += 1
        elif token in ["--disconnect", "-d"]:
            cmd.action = "disconnect"
            index += 1
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            index += 1

    return cmd


class BridgeFeature:
    def __init__(self):
        self.instances: Dict[str, object] = {}
        self.available = True

        try:
            from bridges.bridge_parser import parse_bridge_command
        except ImportError:
            parse_bridge_command = _fallback_parse_bridge_command

        try:
            from bridges.example_bridge import ExampleBridge
            self.bridge_factory = ExampleBridge
        except ImportError:
            self.available = False
            self.bridge_factory = None

        self.parse_bridge_command = parse_bridge_command

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$bridge")

    async def handle_passive(self, app, message: IncomingMessage) -> HandlerResult:
        if message.content.startswith("$"):
            return []

        bridge = self.instances.get(message.channel_id)
        if bridge and bridge.is_listening():
            reply = await bridge.send_message(message.content)
            if reply:
                return [BotResponse(text=reply)]
        return []

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if not self.available:
            return [BotResponse(text="Bridge system not configured. Add a bridge implementation to enable `$bridge` commands.")]

        cmd = self.parse_bridge_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]

        bridge = self.instances.get(message.channel_id)
        if bridge is None:
            bridge = self.bridge_factory(message.channel_id)
            self.instances[message.channel_id] = bridge

        if getattr(cmd, "toggle_listen", False):
            listen_mode = getattr(cmd, "listen_mode", None)
            if listen_mode == "on":
                bridge.set_listen_mode(True)
                return [
                    BotResponse(text="Bridge listen mode: 🟢 ON"),
                    BotResponse(text="💡 Non-command messages will be forwarded to the bridge"),
                ]
            if listen_mode == "off":
                bridge.set_listen_mode(False)
                return [BotResponse(text="Bridge listen mode: 🔴 OFF")]

            bridge.set_listen_mode(not bridge.is_listening())
            responses = [BotResponse(text=f"Bridge listen mode: {'🟢 ON' if bridge.is_listening() else '🔴 OFF'}")]
            if bridge.is_listening():
                responses.append(BotResponse(text="💡 Non-command messages will be forwarded to the bridge"))
            return responses

        if cmd.show_status:
            return [BotResponse(text=bridge.get_status())]

        if cmd.action == "init":
            success = await bridge.initialize()
            return [BotResponse(text="✅ Bridge initialized successfully" if success else "❌ Failed to initialize bridge")]

        if cmd.action == "send":
            if not cmd.message:
                return [BotResponse(text='❌ No message provided. Use --send "your message"')]
            reply = await bridge.send_message(cmd.message)
            if reply:
                return [BotResponse(text=f"📨 Reply: {reply}")]
            return [BotResponse(text="✅ Message sent")]

        if cmd.action == "disconnect":
            success = await bridge.disconnect()
            if success:
                self.instances.pop(message.channel_id, None)
                return [BotResponse(text="✅ Bridge disconnected")]
            return [BotResponse(text="❌ Failed to disconnect bridge")]

        return [BotResponse(text=bridge.get_status())]
