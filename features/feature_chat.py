import json
from dataclasses import replace

from providers import unified_chat
from services import database as db
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_chat_command


class ChatFeature:
    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$chat")

    def _channel_key(self, message: IncomingMessage):
        return message.channel_id

    @staticmethod
    def _creator_context(app, message: IncomingMessage):
        if not message.metadata.get("is_creator"):
            return None
        return app.config.get("CREATOR_PROMPT") or None

    async def handle_passive(self, app, message: IncomingMessage) -> HandlerResult:
        if message.content.startswith("$"):
            return []

        settings = db.get_channel_settings(message.channel_id)
        if settings["listen_mode"]:
            answer = unified_chat.query_chat(
                message.content,
                self._channel_key(message),
                message.created_at,
                message.author_display_name,
                self._creator_context(app, message),
            )
            return [BotResponse(text=answer)]

        return []

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        cmd = parse_chat_command(message.content)
        if cmd.errors:
            error_message = "❌ **Command Errors:**\n" + "\n".join([f"• {err}" for err in cmd.errors])
            return [BotResponse(text=error_message)]

        responses = []

        if cmd.llm:
            responses.append(unified_chat.set_llm(self._channel_key(message), cmd.llm))

        if cmd.model:
            responses.append(unified_chat.set_model(self._channel_key(message), cmd.model))

        if cmd.effort:
            responses.append(unified_chat.set_effort(self._channel_key(message), cmd.effort))

        if cmd.prompt_action:
            if cmd.prompt_action == "list":
                responses.append(unified_chat.get_prompt_list())
            elif cmd.prompt_action == "show":
                llm_name, prompt_content = unified_chat.show_prompt(self._channel_key(message))
                if prompt_content:
                    responses.append(f"**Current prompt for {llm_name}:**\n```\n{prompt_content}\n```")
                else:
                    with open("llm_config.json", "r", encoding="utf-8") as config_file:
                        default_prompt = json.load(config_file)["prompts"][0]
                    responses.append(
                        f"**Using default prompt:** {default_prompt['name']}\n```\n{default_prompt['content']}\n```"
                    )
            elif cmd.prompt_action == "set":
                unified_chat.set_custom_prompt(self._channel_key(message), cmd.prompt_value)
                responses.append("✅ Custom prompt set!\n💡 Consider using `$chat --clear` to start fresh")
            elif cmd.prompt_action.isdigit():
                responses.append(
                    unified_chat.change_prompt(self._channel_key(message), int(cmd.prompt_action), message.created_at)
                )

        if cmd.clear_history:
            unified_chat.clear_history(self._channel_key(message))
            responses.append("✅ Chat history cleared")

        if cmd.listen_mode:
            enabled = cmd.listen_mode == "on"
            db.set_listen_mode(message.channel_id, enabled)
            if enabled:
                responses.append("🟢 **Listen mode enabled**\nI'll respond to all your messages (except $ commands)")
            else:
                responses.append("🔴 **Listen mode disabled**\nUse `$chat --send <message>` to chat")

        if cmd.message:
            agent_message = replace(message, content=f"$agent {cmd.message}")
            agent_responses = await app.agent_feature.handle(app, agent_message)
            responses.extend(response.text for response in agent_responses if response.text)

        if cmd.show_models:
            responses.append(unified_chat.get_models_list())

        if cmd.show_status and not (cmd.message or cmd.show_models or cmd.prompt_action == "list"):
            responses.append(unified_chat.get_status(self._channel_key(message)))

        if not responses:
            responses.append(unified_chat.get_status(self._channel_key(message)))

        return [BotResponse(text="\n\n".join(responses))]
