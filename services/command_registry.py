from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List


ToolHandler = Callable[[Any, Any, Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class CommandTool:
    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]]
    required: List[str]
    risk: str
    handler: ToolHandler

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required,
                "additionalProperties": False,
            },
            "risk": self.risk,
        }


class CommandRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, CommandTool] = {}

    def register(self, tool: CommandTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name: str) -> CommandTool:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")
        return self._tools[name]

    def validate_arguments(self, tool: CommandTool, arguments: Dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ValueError(f"Arguments for {tool.name} must be an object")
        unknown = set(arguments) - set(tool.parameters)
        if unknown:
            raise ValueError(f"Unknown arguments for {tool.name}: {', '.join(sorted(unknown))}")
        missing = [name for name in tool.required if not arguments.get(name)]
        if missing:
            raise ValueError(f"Missing arguments for {tool.name}: {', '.join(missing)}")
        expected_types = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
        for name, value in arguments.items():
            parameter = tool.parameters[name]
            expected = expected_types.get(parameter.get("type"))
            if expected is not None and not isinstance(value, expected):
                raise ValueError(f"Argument {name} for {tool.name} has the wrong type")
            if "enum" in parameter and value not in parameter["enum"]:
                allowed = ", ".join(str(option) for option in parameter["enum"])
                raise ValueError(f"Argument {name} for {tool.name} must be one of: {allowed}")

    async def execute(self, name: str, app, message, arguments: Dict[str, Any]) -> str:
        tool = self.get(name)
        self.validate_arguments(tool, arguments)
        return await tool.handler(app, message, arguments)