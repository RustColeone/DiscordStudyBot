from app.bot_types import BotResponse, HandlerResult, IncomingMessage


class BroadcastFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$broadcast")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        target_channel_id = str(app.config.get("WECHAT_BROADCAST_CHAT") or app.config.get("ID_CHANNEL1", "")).strip()
        if not target_channel_id:
            return [BotResponse(text="❌ Broadcast target is not configured")]

        payload = message.content[len("$broadcast") :].strip()
        if not payload:
            return [BotResponse(text="❌ No broadcast message provided")]

        await app.platform.send_channel_message(target_channel_id, payload)
        return [BotResponse(text="✅ Broadcast sent")]
