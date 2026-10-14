import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.config_service import load_config


class ConfigServiceTests(unittest.TestCase):
    def test_mcp_servers_json_overrides_yaml(self):
        configured = {
            "records": {
                "transport": "streamable_http",
                "url": "http://127.0.0.1:9000/mcp",
                "allow_tools": ["lookup"],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yml"
            config_path.write_text("MCP_SERVERS: {}\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"MCP_SERVERS_JSON": json.dumps(configured)},
                clear=False,
            ):
                config = load_config(str(config_path))

        self.assertEqual(config["MCP_SERVERS"], configured)