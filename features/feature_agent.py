import asyncio
from dataclasses import replace
import json
from typing import Any, Dict, List

from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from providers import unified_chat
from services.command_registry import CommandRegistry, CommandTool


class AgentFeature:
    def __init__(self, time_feature, system_feature, search_feature, help_feature,
                 music_feature=None, clip_feature=None, database_feature=None,
                 bridge_feature=None, broadcast_feature=None):
        self.time_feature = time_feature
        self.system_feature = system_feature
        self.search_feature = search_feature
        self.help_feature = help_feature
        self.music_feature = music_feature
        self.clip_feature = clip_feature
        self.database_feature = database_feature
        self.bridge_feature = bridge_feature
        self.broadcast_feature = broadcast_feature
        self.registry = CommandRegistry()
        self.pending_plans: Dict[str, List[Dict[str, Any]]] = {}
        self._register_tools()

    def _register_tools(self) -> None:
        self.registry.register(CommandTool(
            name="ask_clarification",
            description=(
                "Ask one concise follow-up question when the user's requested action matches a tool "
                "but a required argument cannot be inferred from the request or message context."
            ),
            parameters={"question": {"type": "string", "description": "The focused question to ask"}},
            required=["question"],
            risk="read_only",
            handler=self._ask_clarification,
        ))
        self.registry.register(CommandTool(
            name="system_status",
            description="Report server time, CPU, temperature, memory, and storage.",
            parameters={},
            required=[],
            risk="read_only",
            handler=self._system_status,
        ))
        self.registry.register(CommandTool(
            name="current_time",
            description="Show the bot's configured local time and common world times.",
            parameters={},
            required=[],
            risk="read_only",
            handler=self._current_time,
        ))
        self.registry.register(CommandTool(
            name="google_search",
            description="Search Google for current information.",
            parameters={"query": {"type": "string", "description": "Search query"}},
            required=["query"],
            risk="read_only",
            handler=self._google_search,
        ))
        self.registry.register(CommandTool(
            name="create_reminder",
            description="Create a persistent reminder from a phrase such as 'tomorrow at 8 PM to call Alex'.",
            parameters={"request": {"type": "string", "description": "Time phrase followed by 'to' and the task"}},
            required=["request"],
            risk="write",
            handler=self._create_reminder,
        ))
        self.registry.register(CommandTool(
            name="list_reminders",
            description=(
                "List pending reminders. Use for requests to show, view, inspect, or 查看 reminders; "
                "this does not create a reminder."
            ),
            parameters={"include_all": {"type": "boolean", "description": "Creator: include every user's reminders"}},
            required=[],
            risk="read_only",
            handler=self._list_reminders,
        ))
        self.registry.register(CommandTool(
            name="delete_reminder",
            description="Delete or cancel one pending reminder by its numeric ID.",
            parameters={"reminder_id": {"type": "integer", "description": "Reminder ID to delete"}},
            required=["reminder_id"],
            risk="write",
            handler=self._delete_reminder,
        ))
        self.registry.register(CommandTool(
            name="command_help",
            description="Show the bot command reference or help for a topic.",
            parameters={"topic": {"type": "string", "description": "Optional help topic"}},
            required=[],
            risk="read_only",
            handler=self._command_help,
        ))
        self.registry.register(CommandTool(
            name="wolfram_query",
            description="Calculate or answer a factual query with Wolfram Alpha.",
            parameters={"query": {"type": "string", "description": "Question or calculation"}},
            required=["query"],
            risk="read_only",
            handler=self._wolfram_query,
        ))
        if self.database_feature is not None:
            self.registry.register(CommandTool(
                name="database_stats",
                description="Show stored chat and music database statistics.",
                parameters={},
                required=[],
                risk="read_only",
                handler=self._database_stats,
            ))
            self.registry.register(CommandTool(
                name="export_database",
                description="Export the bot database to its configured JSON backup file.",
                parameters={},
                required=[],
                risk="write",
                handler=self._export_database,
            ))
        if self.music_feature is not None:
            self.registry.register(CommandTool(
                name="music_status",
                description="Show the current music player track and position.",
                parameters={},
                required=[],
                risk="read_only",
                handler=self._music_status,
            ))
            self.registry.register(CommandTool(
                name="music_control",
                description="Initialize, play, pause, stop, or change tracks in the Discord music player.",
                parameters={"action": {
                    "type": "string",
                    "enum": ["initialize", "play", "pause", "stop", "next", "previous"],
                    "description": "Music player action",
                }},
                required=["action"],
                risk="write",
                handler=self._music_control,
            ))
            self.registry.register(CommandTool(
                name="queue_music",
                description=(
                    "Add a YouTube music URL to the current playlist queue. Resolve references such as "
                    "'that music' from recent conversation and pass its URL."
                ),
                parameters={"url": {"type": "string", "description": "YouTube URL to add"}},
                required=["url"],
                risk="write",
                handler=self._queue_music,
            ))
        if self.clip_feature is not None:
            self.registry.register(CommandTool(
                name="clip_video",
                description=(
                    "Prepare a video clip from a URL. Resolve 'this video' from recent conversation. "
                    "The user receives a preview before media is processed."
                ),
                parameters={
                    "url": {"type": "string", "description": "Source video URL"},
                    "start": {"type": "string", "description": "Optional start time, such as 1:05"},
                    "end": {"type": "string", "description": "Optional end time, such as 1:20"},
                    "format": {"type": "string", "description": "Optional output format, such as mp4, gif, or mp3"},
                },
                required=["url"],
                risk="write",
                handler=self._clip_video,
            ))
            self.registry.register(CommandTool(
                name="manage_pending_clips",
                description="Confirm processing or cancel the pending video clip preview.",
                parameters={"action": {
                    "type": "string",
                    "enum": ["confirm", "cancel"],
                    "description": "Pending clip action",
                }},
                required=["action"],
                risk="write",
                handler=self._manage_pending_clips,
            ))
        self.registry.register(CommandTool(
            name="live_clock_control",
            description="Start or stop the live updating world clock in the current channel.",
            parameters={"action": {
                "type": "string",
                "enum": ["start", "stop"],
                "description": "Live clock action",
            }},
            required=["action"],
            risk="write",
            handler=self._live_clock_control,
        ))
        if self.bridge_feature is not None:
            self.registry.register(CommandTool(
                name="bridge_status",
                description="Show the external messaging bridge status.",
                parameters={},
                required=[],
                risk="read_only",
                handler=self._bridge_status,
            ))
            self.registry.register(CommandTool(
                name="bridge_control",
                description="Initialize, disconnect, configure, or send a message through the external bridge.",
                parameters={
                    "action": {
                        "type": "string",
                        "enum": ["initialize", "disconnect", "listen_on", "listen_off", "send"],
                        "description": "Bridge action",
                    },
                    "message": {"type": "string", "description": "Message required when action is send"},
                },
                required=["action"],
                risk="write",
                handler=self._bridge_control,
            ))
        if self.broadcast_feature is not None:
            self.registry.register(CommandTool(
                name="broadcast_message",
                description="Send a message to the configured broadcast channel.",
                parameters={"message": {"type": "string", "description": "Message to broadcast"}},
                required=["message"],
                risk="write",
                handler=self._broadcast_message,
            ))

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content == "$agent" or message.content.startswith("$agent ")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        request = message.content[len("$agent"):].strip()
        planning_request = self._with_reference_context(request, message)
        key = f"{message.channel_id}:{message.author_id}"

        if request.lower() == "tools":
            lines = [f"- `{tool['name']}`: {tool['description']}" for tool in self.registry.schemas()]
            return [BotResponse(text="**Available agent tools**\n" + "\n".join(lines))]
        if request.lower() == "cancel":
            self.pending_plans.pop(key, None)
            return [BotResponse(text="Pending agent plan cancelled.")]
        if request.lower() == "confirm":
            plan = self.pending_plans.pop(key, None)
            if plan is None:
                return [BotResponse(text="There is no pending agent plan to confirm.")]
            progress = await self._start_progress(app, message, "Starting the confirmed actions...")
            try:
                return await self._execute_plan(app, message, plan, progress)
            finally:
                await self._finish_progress(app, progress)
        if not request:
            return [BotResponse(text="Use `$agent tools` or mention me with an action request.")]

        progress = await self._start_progress(
            app,
            message,
            "Understanding your request and checking available capabilities...",
        )
        loop = asyncio.get_running_loop()
        try:
            try:
                plan = await loop.run_in_executor(
                    None,
                    unified_chat.plan_agent_actions,
                    planning_request,
                    self.registry.schemas(),
                    message.channel_id,
                    message.created_at,
                )
                self._validate_plan(plan)
            except Exception as error:
                return [BotResponse(text=f"I couldn't build a valid action plan: {error}")]

            if not plan:
                await self._update_progress(app, progress, "No tool is needed; composing a response...")
                answer = await loop.run_in_executor(
                    None,
                    unified_chat.query_chat,
                    request,
                    message.channel_id,
                    message.created_at,
                    message.author_display_name,
                    app.config.get("CREATOR_PROMPT") if message.metadata.get("is_creator") else None,
                )
                return [BotResponse(text=answer)]

            tool_names = self._format_tool_names(plan)
            if any(self.registry.get(step["tool"]).risk != "read_only" for step in plan):
                await self._update_progress(
                    app,
                    progress,
                    f"Selected {tool_names}; checking details and preparing confirmation...",
                )
                self.pending_plans[key] = plan
                return [BotResponse(text=self._format_plan(plan) + "\nReply with `$agent confirm` or `$agent cancel`.")]

            await self._update_progress(app, progress, f"Selected {tool_names}; retrieving the result...")
            return await self._execute_plan(app, message, plan, progress)
        finally:
            await self._finish_progress(app, progress)

    @staticmethod
    def _with_reference_context(request: str, message: IncomingMessage) -> str:
        context = []
        referenced_content = message.metadata.get("referenced_content")
        if referenced_content:
            context.append(f"Replied-to message: {referenced_content}")
        referenced_urls = message.metadata.get("referenced_attachment_urls") or []
        if referenced_urls:
            context.append("Replied-to attachments: " + ", ".join(referenced_urls))
        attachment_urls = message.metadata.get("attachment_urls") or []
        if attachment_urls:
            context.append("Current attachments: " + ", ".join(attachment_urls))
        if not context:
            return request
        return request + "\n\nMessage context:\n" + "\n".join(context)

    def _validate_plan(self, plan: List[Dict[str, Any]]) -> None:
        if not isinstance(plan, list):
            raise ValueError("the planner did not return an action list")
        if len(plan) > 5:
            raise ValueError("plans are limited to five actions")
        for step in plan:
            if not isinstance(step, dict) or set(step) != {"tool", "arguments"}:
                raise ValueError("each action must contain only tool and arguments")
            tool = self.registry.get(step["tool"])
            self.registry.validate_arguments(tool, step["arguments"])

    def _format_plan(self, plan: List[Dict[str, Any]]) -> str:
        lines = [f"{index}. `{step['tool']}` {json.dumps(step['arguments'])}" for index, step in enumerate(plan, 1)]
        return "**Planned actions**\n" + "\n".join(lines)

    @staticmethod
    def _format_tool_names(plan: List[Dict[str, Any]]) -> str:
        return ", ".join(f"`{step['tool'].replace('_', ' ')}`" for step in plan)

    @staticmethod
    async def _start_progress(app, message: IncomingMessage, status: str):
        platform = getattr(app, "platform", None)
        if not getattr(platform, "supports_progress", False):
            return None
        try:
            return await platform.send_channel_message(message.channel_id, f"Thinking: {status}")
        except Exception as error:
            print(f"Failed to start agent progress: {error}")
            return None

    @staticmethod
    async def _update_progress(app, progress, status: str) -> None:
        if progress is None:
            return
        try:
            await app.platform.edit_message(progress, f"Thinking: {status}")
        except Exception as error:
            print(f"Failed to update agent progress: {error}")

    @staticmethod
    async def _finish_progress(app, progress) -> None:
        if progress is None:
            return
        try:
            await app.platform.delete_message(progress)
        except Exception as error:
            print(f"Failed to remove agent progress: {error}")

    async def _execute_plan(self, app, message, plan: List[Dict[str, Any]], progress=None) -> HandlerResult:
        outputs: HandlerResult = []
        for index, step in enumerate(plan, 1):
            try:
                tool_name = step["tool"].replace("_", " ")
                await self._update_progress(
                    app,
                    progress,
                    f"Running {tool_name} ({index}/{len(plan)})...",
                )
                result = await self.registry.execute(step["tool"], app, message, step["arguments"])
                if isinstance(result, str):
                    outputs.append(BotResponse(text=f"**{index}. {step['tool']}**\n{result}"))
                else:
                    for response in result:
                        if response.text:
                            response.text = f"**{index}. {step['tool']}**\n{response.text}"
                        outputs.append(response)
            except Exception as error:
                outputs.append(BotResponse(text=f"**{index}. {step['tool']} failed:** {error}"))
                break
        return outputs

    async def _system_status(self, app, message, arguments) -> str:
        responses = await self.system_feature.handle(app, replace(message, content="$system"))
        return responses[0].text or "No system status returned."

    async def _ask_clarification(self, app, message, arguments) -> str:
        return arguments["question"]

    async def _current_time(self, app, message, arguments) -> str:
        responses = await self.time_feature.handle(app, replace(message, content="$time"))
        return responses[0].text or "No time returned."

    async def _google_search(self, app, message, arguments) -> str:
        query = json.dumps(arguments["query"])
        responses = await self.search_feature.handle(app, replace(message, content=f"$google -s {query}"))
        return "\n".join(response.text or "" for response in responses)

    async def _wolfram_query(self, app, message, arguments) -> str:
        query = json.dumps(arguments["query"])
        responses = await self.search_feature.handle(app, replace(message, content=f"$wolfram -q {query}"))
        return "\n".join(response.text or "" for response in responses)

    async def _create_reminder(self, app, message, arguments) -> str:
        responses = self.time_feature.create_natural_reminder(
            app,
            message,
            f"$remind {arguments['request']}",
        )
        return responses[0].text or "Reminder created."

    async def _list_reminders(self, app, message, arguments) -> str:
        return self.time_feature.list_reminders(message, bool(arguments.get("include_all", False)))

    async def _delete_reminder(self, app, message, arguments) -> str:
        return self.time_feature.delete_reminder(message, arguments["reminder_id"])

    async def _database_stats(self, app, message, arguments):
        return await self.database_feature.handle(app, replace(message, content="$db --stats"))

    async def _export_database(self, app, message, arguments):
        return await self.database_feature.handle(app, replace(message, content="$db --export"))

    async def _music_status(self, app, message, arguments):
        return await self.music_feature.handle(app, replace(message, content="$music --name"))

    async def _music_control(self, app, message, arguments):
        flags = {
            "initialize": "--init",
            "play": "--play",
            "pause": "--pause",
            "stop": "--stop",
            "next": "--next",
            "previous": "--previous",
        }
        return await self.music_feature.handle(
            app,
            replace(message, content=f"$music {flags[arguments['action']]}")
        )

    async def _queue_music(self, app, message, arguments):
        content = f"$music --youtube {json.dumps(arguments['url'])} --queue"
        return await self.music_feature.handle(app, replace(message, content=content))

    async def _clip_video(self, app, message, arguments):
        content = f"$clip --url {json.dumps(arguments['url'])}"
        for name in ("start", "end", "format"):
            if arguments.get(name):
                content += f" --{name} {json.dumps(arguments[name])}"
        return await self.clip_feature.handle(app, replace(message, content=content))

    async def _manage_pending_clips(self, app, message, arguments):
        return await self.clip_feature.handle(
            app,
            replace(message, content=f"$clip --{arguments['action']}")
        )

    async def _live_clock_control(self, app, message, arguments):
        return await self.time_feature.handle(app, replace(message, content=f"${arguments['action']}"))

    async def _bridge_status(self, app, message, arguments):
        return await self.bridge_feature.handle(app, replace(message, content="$bridge --status"))

    async def _bridge_control(self, app, message, arguments):
        action = arguments["action"]
        if action == "send":
            if not arguments.get("message"):
                raise ValueError("bridge send requires a message")
            content = f"$bridge --send {json.dumps(arguments['message'])}"
        else:
            flags = {
                "initialize": "--init",
                "disconnect": "--disconnect",
                "listen_on": "--listen on",
                "listen_off": "--listen off",
            }
            content = f"$bridge {flags[action]}"
        return await self.bridge_feature.handle(app, replace(message, content=content))

    async def _broadcast_message(self, app, message, arguments):
        return await self.broadcast_feature.handle(
            app,
            replace(message, content=f"$broadcast {arguments['message']}")
        )

    async def _command_help(self, app, message, arguments) -> str:
        topic = arguments.get("topic", "").strip()
        content = "$help" + (f" {topic}" if topic else "")
        responses = await self.help_feature.handle(app, replace(message, content=content))
        return responses[0].text or "No help returned."