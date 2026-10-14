import asyncio
import base64
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
import mimetypes
import os
import re
import tempfile
from typing import Any, Callable, Dict, List

from app.bot_types import BotAttachment, BotResponse, HandlerResult
from jsonschema import ValidationError, validate, validators
from mcp import Client, StdioServerParameters

from services.command_registry import CommandRegistry, CommandTool


MAX_RESULT_CHARS = 60_000
MAX_BINARY_BYTES = 20_000_000
VALID_RISKS = {"read_only", "prepare", "write"}


@dataclass(frozen=True)
class MCPToolBinding:
    exposed_name: str
    server_name: str
    remote_name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any] | None
    risk: str


class MCPToolManager:
    def __init__(
        self,
        server_configs: Dict[str, Dict[str, Any]] | None,
        client_factory: Callable[..., Any] = Client,
    ) -> None:
        self.server_configs = server_configs or {}
        self.client_factory = client_factory
        self.clients: Dict[str, Any] = {}
        self.bindings: Dict[str, MCPToolBinding] = {}
        self.errors: Dict[str, str] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._registry: CommandRegistry | None = None
        self._runner: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None

    async def connect(self, registry: CommandRegistry) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._registry = registry
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._runner = asyncio.create_task(self._run())
        await self._ready.wait()

    async def close(self) -> None:
        if self._runner is None:
            return
        self._stop.set()
        await self._runner
        self._runner = None

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as exit_stack:
                self._exit_stack = exit_stack
                for server_name, settings in self.server_configs.items():
                    if not self._enabled(settings):
                        continue
                    try:
                        target = self._client_target(settings)
                        client = self.client_factory(
                            target,
                            read_timeout_seconds=float(settings.get("timeout_seconds", 30)),
                        )
                        connect_timeout = float(settings.get("connect_timeout_seconds", 15))
                        async with asyncio.timeout(connect_timeout):
                            self.clients[server_name] = await exit_stack.enter_async_context(client)
                        await self._refresh_server(server_name, settings)
                    except Exception as error:
                        self.errors[server_name] = str(error)
                        print(f"MCP server {server_name} unavailable: {error}")
                self._ready.set()
                await self._stop.wait()
        finally:
            self.clients.clear()
            self._exit_stack = None
            self._ready.set()

    async def refresh(self) -> None:
        for server_name, client in list(self.clients.items()):
            del client
            settings = self.server_configs[server_name]
            try:
                await self._refresh_server(server_name, settings)
                self.errors.pop(server_name, None)
            except Exception as error:
                self.errors[server_name] = str(error)
                print(f"MCP tool refresh failed for {server_name}: {error}")

    async def call(self, exposed_name: str, arguments: Dict[str, Any]) -> HandlerResult:
        binding = self.bindings.get(exposed_name)
        if binding is None:
            raise ValueError(f"Unknown MCP tool: {exposed_name}")
        client = self.clients.get(binding.server_name)
        if client is None:
            raise RuntimeError(f"MCP server {binding.server_name} is not connected")
        timeout = float(self.server_configs[binding.server_name].get("timeout_seconds", 30))
        result = await asyncio.wait_for(
            client.call_tool(binding.remote_name, arguments),
            timeout=timeout,
        )
        if getattr(result, "is_error", False):
            detail = self._error_text(result)
            raise RuntimeError(detail or f"MCP tool {binding.remote_name} failed")
        structured = getattr(result, "structured_content", None)
        if binding.output_schema is not None and structured is None:
            raise RuntimeError(
                f"MCP tool {binding.remote_name} did not return its declared structured content"
            )
        if binding.output_schema is not None:
            try:
                validate(structured, binding.output_schema)
            except ValidationError as error:
                raise RuntimeError(
                    f"MCP tool {binding.remote_name} returned invalid structured content: "
                    f"{error.message}"
                ) from error
        responses = self._normalize_result(result)
        print(f"MCP tool completed: {binding.server_name}/{binding.remote_name}")
        return responses

    async def _refresh_server(self, server_name: str, settings: Dict[str, Any]) -> None:
        client = self.clients[server_name]
        remote_tools = []
        cursor = None
        while True:
            result = await client.list_tools(cursor=cursor)
            remote_tools.extend(result.tools)
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break

        allowed = settings.get("allow_tools") or []
        allowed_names = {str(name) for name in allowed}
        allow_all = "*" in allowed_names
        risk_map = settings.get("risk") or {}
        refreshed = set()
        for remote_tool in remote_tools:
            if not allow_all and remote_tool.name not in allowed_names:
                continue
            exposed_name = self._exposed_name(server_name, remote_tool.name)
            risk = str(risk_map.get(remote_tool.name, "write"))
            if risk not in VALID_RISKS:
                raise ValueError(
                    f"MCP tool {server_name}/{remote_tool.name} has invalid risk {risk}"
                )
            binding = MCPToolBinding(
                exposed_name=exposed_name,
                server_name=server_name,
                remote_name=remote_tool.name,
                description=(remote_tool.description or remote_tool.title or remote_tool.name),
                input_schema=dict(remote_tool.input_schema or {"type": "object"}),
                output_schema=(
                    dict(remote_tool.output_schema)
                    if remote_tool.output_schema is not None else None
                ),
                risk=risk,
            )
            validators.validator_for(binding.input_schema).check_schema(binding.input_schema)
            if binding.output_schema is not None:
                validators.validator_for(binding.output_schema).check_schema(binding.output_schema)
            self.bindings[exposed_name] = binding
            self._registry.register(self._command_tool(binding), replace=True)
            refreshed.add(exposed_name)

        stale = {
            name for name, binding in self.bindings.items()
            if binding.server_name == server_name and name not in refreshed
        }
        for name in stale:
            self.bindings.pop(name, None)
            self._registry.unregister(name)

    def _command_tool(self, binding: MCPToolBinding) -> CommandTool:
        async def handler(app, message, arguments):
            del app, message
            return await self.call(binding.exposed_name, arguments)

        return CommandTool(
            name=binding.exposed_name,
            description=f"MCP {binding.server_name}: {binding.description}",
            parameters={},
            required=[],
            risk=binding.risk,
            handler=handler,
            input_schema=binding.input_schema,
        )

    @staticmethod
    def _enabled(settings: Any) -> bool:
        return isinstance(settings, dict) and bool(settings.get("enabled", True))

    @staticmethod
    def _client_target(settings: Dict[str, Any]):
        transport = str(settings.get("transport", "streamable_http")).lower()
        if transport in {"streamable_http", "http"}:
            url = str(settings.get("url", "")).strip()
            if not url:
                raise ValueError("streamable_http MCP configuration requires url")
            return url
        if transport != "stdio":
            raise ValueError(f"Unsupported MCP transport: {transport}")
        command = str(settings.get("command", "")).strip()
        if not command:
            raise ValueError("stdio MCP configuration requires command")
        environment = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "LANG", "LC_ALL")
            if name in os.environ
        }
        for name in settings.get("env_passthrough") or []:
            if name in os.environ:
                environment[str(name)] = os.environ[str(name)]
        environment.update({str(key): str(value) for key, value in (settings.get("env") or {}).items()})
        return StdioServerParameters(
            command=command,
            args=[str(value) for value in settings.get("args") or []],
            env=environment,
            cwd=settings.get("cwd"),
        )

    @staticmethod
    def _exposed_name(server_name: str, tool_name: str) -> str:
        normalize = lambda value: re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
        return f"mcp__{normalize(server_name)}__{normalize(tool_name)}"

    @classmethod
    def _normalize_result(cls, result: Any) -> HandlerResult:
        responses: HandlerResult = []
        text_parts: List[str] = []
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            text_parts.append(json.dumps(structured, ensure_ascii=False, default=str))
        for block in getattr(result, "content", []) or []:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(str(block.text))
            elif block_type in {"image", "audio"}:
                responses.append(cls._binary_response(block))
            elif block_type == "resource_link":
                text_parts.append(f"Resource: {block.name} ({block.uri})")
            elif block_type == "resource":
                resource = block.resource
                text = getattr(resource, "text", None)
                uri = getattr(resource, "uri", "embedded resource")
                text_parts.append(f"Resource {uri}:\n{text or '[binary content omitted]'}")
        if text_parts:
            responses.insert(0, BotResponse(
                text="\n\n".join(text_parts)[:MAX_RESULT_CHARS],
                metadata={"tool_evidence": True},
            ))
        return responses or [BotResponse(text="MCP tool completed without content.")]

    @staticmethod
    def _error_text(result: Any) -> str:
        return "\n\n".join(
            str(block.text)
            for block in getattr(result, "content", []) or []
            if getattr(block, "type", "") == "text"
        )[:MAX_RESULT_CHARS]

    @staticmethod
    def _binary_response(block: Any) -> BotResponse:
        mime_type = str(getattr(block, "mime_type", "application/octet-stream"))
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        if len(block.data) > ((MAX_BINARY_BYTES + 2) // 3) * 4:
            raise RuntimeError("MCP binary result exceeds the 20 MB limit")
        content = base64.b64decode(block.data, validate=True)
        if len(content) > MAX_BINARY_BYTES:
            raise RuntimeError("MCP binary result exceeds the 20 MB limit")
        with tempfile.NamedTemporaryFile(prefix="discord-mcp-", suffix=suffix, delete=False) as output:
            output.write(content)
        return BotResponse(
            text=f"MCP returned {mime_type} content.",
            attachment=BotAttachment(
                path=output.name,
                filename=f"mcp-result{suffix}",
                delete_after_send=True,
            ),
            metadata={"tool_evidence": True},
        )