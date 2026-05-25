"""Tests for AnthropicProvider tool schema conversion."""

import unittest

from src.providers.anthrophic_provider import AnthropicProvider
from src.tools.exec_tool import ExecTool


class AnthropicToolSchemaTest(unittest.TestCase):
    def test_exec_schema_preserves_mode_and_stdio_enums(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-test")

        schema = provider._convert_to_anthropic_tool_schema(ExecTool())
        properties = schema["input_schema"]["properties"]

        self.assertEqual(properties["mode"]["type"], "string")
        self.assertEqual(
            properties["mode"]["enum"],
            ["one_shot", "session", "daemon"],
        )
        self.assertEqual(properties["stdio"]["type"], "string")
        self.assertEqual(properties["stdio"]["enum"], ["auto", "pty", "pipe"])


if __name__ == "__main__":
    unittest.main()
