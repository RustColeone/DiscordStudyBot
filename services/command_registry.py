from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from jsonschema import ValidationError, validate


ToolHandler = Callable[[Any, Any, Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class CommandTool:
    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]]
    required: List[str]
    risk: str
    handler: ToolHandler
    input_schema: Optional[Dict[str, Any]] = None

    def argument_schema(self) -> Dict[str, Any]:
        if self.input_schema is not None:
            return self.input_schema
        return {
            "type": "object",
            "properties": self.parameters,
            "required": self.required,
            "additionalProperties": False,
        }

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.argument_schema(),
            "risk": self.risk,
        }


class CommandRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, CommandTool] = {}

    def register(self, tool: CommandTool, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name: str) -> CommandTool:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")
        return self._tools[name]

    def validate_arguments(self, tool: CommandTool, arguments: Dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ValueError(f"Arguments for {tool.name} must be an object")
        if tool.input_schema is None:
            missing = [name for name in tool.required if not arguments.get(name)]
            if missing:
                raise ValueError(f"Missing arguments for {tool.name}: {', '.join(missing)}")
        try:
            validate(arguments, tool.argument_schema())
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path)
            detail = f" at {location}" if location else ""
            raise ValueError(
                f"Invalid arguments for {tool.name}{detail}: {error.message}"
            ) from error

    async def execute(self, name: str, app, message, arguments: Dict[str, Any]) -> Any:
        tool = self.get(name)
        self.validate_arguments(tool, arguments)
        return await tool.handler(app, message, arguments)
