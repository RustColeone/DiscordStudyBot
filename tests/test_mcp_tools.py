from types import SimpleNamespace
import unittest

from services.command_registry import CommandRegistry
from services.mcp_tools import MCPToolManager


class FakeMCPClient:
    instances = []

    def __init__(self, target, **options):
        self.target = target
        self.options = options
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.closed = True

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            tools=[SimpleNamespace(
                name="lookup",
                title="Lookup",
                description="Look up one record",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "options": {
                            "type": "object",
                            "properties": {"limit": {"type": "integer", "minimum": 1}},
                            "required": ["limit"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["query", "options"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            )],
            next_cursor=None,
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="record found")],
            structured_content={"count": 1},
            is_error=False,
        )


class MCPToolManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeMCPClient.instances.clear()
        self.registry = CommandRegistry()
        self.manager = MCPToolManager(
            {
                "records": {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1:9000/mcp",
                    "allow_tools": ["lookup"],
                    "risk": {"lookup": "read_only"},
                }
            },
            client_factory=FakeMCPClient,
        )
        await self.manager.connect(self.registry)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_discovers_namespaced_tool_and_calls_server(self):
        schema = self.registry.get("mcp__records__lookup").schema()

        self.assertEqual(schema["risk"], "read_only")
        self.assertEqual(schema["parameters"]["properties"]["options"]["type"], "object")
        responses = await self.registry.execute(
            "mcp__records__lookup",
            None,
            None,
            {"query": "alpha", "options": {"limit": 2}},
        )

        self.assertEqual(
            FakeMCPClient.instances[0].calls,
            [("lookup", {"query": "alpha", "options": {"limit": 2}})],
        )
        self.assertIn('"count": 1', responses[0].text)
        self.assertIn("record found", responses[0].text)

    async def test_nested_arguments_are_validated_before_call(self):
        with self.assertRaisesRegex(ValueError, "less than the minimum of 1"):
            await self.registry.execute(
                "mcp__records__lookup",
                None,
                None,
                {"query": "alpha", "options": {"limit": 0}},
            )

        self.assertEqual(FakeMCPClient.instances[0].calls, [])

    async def test_unallowlisted_tools_are_not_exposed(self):
        manager = MCPToolManager(
            {
                "records": {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1:9000/mcp",
                    "allow_tools": [],
                }
            },
            client_factory=FakeMCPClient,
        )
        registry = CommandRegistry()
        await manager.connect(registry)
        self.addAsyncCleanup(manager.close)

        self.assertEqual(registry.schemas(), [])