import asyncio
from dataclasses import replace
import json
from typing import Any, Dict, List

from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from providers import unified_chat
from services.command_registry import CommandRegistry, CommandTool


class AgentFeature:
    def __init__(self, time_feature, system_feature, search_feature, help_feature):
        self.time_feature = time_feature
        self.system_feature = system_feature
        self.search_feature = search_feature
        self.help_feature = help_feature
        self.registry = CommandRegistry()
        self.pending_plans: Dict[str, List[Dict[str, Any]]] = {}
        self._register_tools()

    def _register_tools(self) -> None:
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
            name="command_help",
            description="Show the bot command reference or help for a topic.",
            parameters={"topic": {"type": "string", "description": "Optional help topic"}},
            required=[],
            risk="read_only",
            handler=self._command_help,
        ))

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content == "$agent" or message.content.startswith("$agent ")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        request = message.content[len("$agent"):].strip()
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
            return [BotResponse(text=await self._execute_plan(app, message, plan))]
        if not request:
            return [BotResponse(text="Use `$agent tools` or mention me with an action request.")]

        if request.lower().startswith("remind me "):
            return self.time_feature.create_natural_reminder(app, message, request)

        loop = asyncio.get_running_loop()
        try:
            plan = await loop.run_in_executor(
                None,
                unified_chat.plan_agent_actions,
                request,
                self.registry.schemas(),
                message.channel_id,
                message.created_at,
            )
            self._validate_plan(plan)
        except Exception as error:
            return [BotResponse(text=f"I couldn't build a valid action plan: {error}")]

        if any(self.registry.get(step["tool"]).risk != "read_only" for step in plan):
            self.pending_plans[key] = plan
            return [BotResponse(text=self._format_plan(plan) + "\nReply with `$agent confirm` or `$agent cancel`.")]
        return [BotResponse(text=await self._execute_plan(app, message, plan))]

    def _validate_plan(self, plan: List[Dict[str, Any]]) -> None:
        if not isinstance(plan, list) or not plan:
            raise ValueError("the planner returned no actions")
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

    async def _execute_plan(self, app, message, plan: List[Dict[str, Any]]) -> str:
        outputs = []
        for index, step in enumerate(plan, 1):
            try:
                result = await self.registry.execute(step["tool"], app, message, step["arguments"])
                outputs.append(f"**{index}. {step['tool']}**\n{result}")
            except Exception as error:
                outputs.append(f"**{index}. {step['tool']} failed:** {error}")
                break
        return "\n\n".join(outputs)

    async def _system_status(self, app, message, arguments) -> str:
        responses = await self.system_feature.handle(app, replace(message, content="$system"))
        return responses[0].text or "No system status returned."

    async def _current_time(self, app, message, arguments) -> str:
        responses = await self.time_feature.handle(app, replace(message, content="$time"))
        return responses[0].text or "No time returned."

    async def _google_search(self, app, message, arguments) -> str:
        query = json.dumps(arguments["query"])
        responses = await self.search_feature.handle(app, replace(message, content=f"$google -s {query}"))
        return "\n".join(response.text or "" for response in responses)

    async def _create_reminder(self, app, message, arguments) -> str:
        responses = self.time_feature.create_natural_reminder(
            app,
            message,
            f"$remind {arguments['request']}",
        )
        return responses[0].text or "Reminder created."

    async def _command_help(self, app, message, arguments) -> str:
        topic = arguments.get("topic", "").strip()
        content = "$help" + (f" {topic}" if topic else "")
        responses = await self.help_feature.handle(app, replace(message, content=content))
        return responses[0].text or "No help returned."