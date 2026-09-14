import json
import asyncio
from dataclasses import replace

from providers import unified_chat
from services import database as db
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_chat_command
from services.generation_activity import show_generation_typing


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
            if settings.get("mode", "roleplay") == "agent":
                return await app.agent_feature.handle(
                    app,
                    replace(message, content=f"$agent {message.content}"),
                )
            loop = asyncio.get_running_loop()
            images = (
                (message.metadata.get("referenced_image_attachments") or [])
                + (message.metadata.get("image_attachments") or [])
            )
            async with show_generation_typing(message):
                answer = await loop.run_in_executor(
                    None,
                    unified_chat.query_chat,
                    message.content,
                    self._channel_key(message),
                    message.created_at,
                    message.author_display_name,
                    self._creator_context(app, message),
                    True,
                    images,
                )
            return [BotResponse(text=answer)]

        return []

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        cmd = parse_chat_command(message.content)
        if cmd.errors:
            error_message = "❌ **Command Errors:**\n" + "\n".join([f"• {err}" for err in cmd.errors])
            return [BotResponse(text=error_message)]

        responses = []

        effective_mode = cmd.mode or db.get_channel_settings(self._channel_key(message))["mode"]
        configuration_change = (
            cmd.llm is not None
            or cmd.model is not None
            or cmd.effort is not None
            or cmd.prompt_action == "set"
            or bool(cmd.prompt_action and cmd.prompt_action.isdigit())
        )
        if configuration_change and effective_mode == "agent":
            return [BotResponse(
                text="Agent team models, effort, and personalities are server-admin configured. "
                "Switch to `$chat --mode roleplay` to customize a role-play session."
            )]

        roleplay_change = cmd.llm or cmd.model or cmd.effort or (
            cmd.prompt_action not in {None, "list", "show"}
        )
        if roleplay_change and effective_mode != "roleplay":
            return [BotResponse(
                text="Provider, model, effort, and persona settings belong to role-play mode. "
                "Switch with `$chat --mode roleplay` first."
            )]

        if cmd.mode:
            db.set_chat_mode(self._channel_key(message), cmd.mode)
            responses.append(f"Chat mode set to **{cmd.mode}** for this channel.")

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
                if effective_mode == "agent":
                    responses.append(
                        "Collaborative agent mode uses server-admin configured team personalities: "
                        "DeepSeek and Gemini are juniors; Luna is student council president, and Claude serves on the "
                        "disciplinary committee. "
                        "Role-play prompts are inactive."
                    )
                    cmd.prompt_action = None
                else:
                    llm_name, prompt_content = unified_chat.show_prompt(self._channel_key(message))
                    if prompt_content:
                        responses.append(f"**Current prompt for {llm_name}:**\n```\n{prompt_content}\n```")
                    else:
                        default_prompt = unified_chat.get_default_prompt(llm_name)
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
            agent_message = replace(
                message,
                content=f"$agent {cmd.message}",
                metadata={**message.metadata, "show_trace": cmd.show_trace},
            )
            agent_responses = await app.agent_feature.handle(app, agent_message)
            if responses and agent_responses:
                agent_responses[0].text = "\n\n".join(
                    [*responses, agent_responses[0].text or ""]
                ).strip()
            return agent_responses

        if cmd.show_models:
            responses.append(unified_chat.get_models_list())

        if cmd.show_status and not (cmd.message or cmd.show_models or cmd.prompt_action == "list"):
            responses.append(unified_chat.get_status(self._channel_key(message)))

        if not responses:
            responses.append(unified_chat.get_status(self._channel_key(message)))

        return [BotResponse(text="\n\n".join(responses))]
