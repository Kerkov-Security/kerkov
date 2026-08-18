"""Tests for AnthropicProvider tool schema conversion."""

import os
import tempfile
import unittest

import anthropic

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.skill_tool import SkillTool
from kerkov.tools.tool_suite import ToolSuite


def _make_provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-test")


def _make_active_tools():
    with tempfile.TemporaryDirectory() as skills_dir:
        with tempfile.TemporaryDirectory() as agents_dir:
            skill_manager = SkillManager(skills_dir=skills_dir)
            agent_manager = AgentManager(agents_dir=agents_dir)
            registry = AgentRegistry()
            supervisor = AgentSupervisor(registry)
            tools = ToolSuite.get_tools()
            tools.append(SkillTool(skill_manager))
            tools.append(
                SpawnAgentTool(
                    agent_manager=agent_manager,
                    master_tools=tools,
                    provider_getter=lambda: None,
                    registry=registry,
                    supervisor=supervisor,
                )
            )
            tools.append(AgentWaitTool(supervisor))
            return tools


class AnthropicToolSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_exec_schema_preserves_mode_and_stdio_enums(self):
        schema = self.provider._convert_to_anthropic_tool_schema(ExecTool())
        properties = schema["input_schema"]["properties"]

        self.assertEqual(properties["mode"]["type"], "string")
        self.assertEqual(
            properties["mode"]["enum"],
            ["one_shot", "session", "daemon"],
        )
        self.assertEqual(properties["stdio"]["type"], "string")
        self.assertEqual(properties["stdio"]["enum"], ["auto", "pty", "pipe"])

    def test_http_schema_preserves_maps_and_any_without_string_coercion(self):
        schema = self.provider._convert_to_anthropic_tool_schema(HTTPTool())
        properties = schema["input_schema"]["properties"]

        self.assertEqual(properties["headers"]["type"], "object")
        self.assertEqual(
            properties["headers"]["additionalProperties"],
            {"type": "string"},
        )
        self.assertTrue(properties["params"]["additionalProperties"])
        self.assertNotIn("anyOf", properties["json"])
        self.assertNotEqual(properties["json"].get("type"), "string")
        self.assertEqual(
            properties["json"]["description"],
            "JSON body. Sets Content-Type: application/json automatically. "
            "Mutually exclusive with `data`.",
        )

    def test_clean_schema_resolves_optional_list_of_refs_recursively(self):
        defs = {
            "Item": {
                "title": "Item",
                "type": "object",
                "properties": {
                    "name": {"title": "Name", "type": "string"},
                    "tags": {
                        "title": "Tags",
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["name"],
            }
        }
        schema = {
            "anyOf": [
                {"type": "array", "items": {"$ref": "#/$defs/Item"}},
                {"type": "null"},
            ],
            "default": None,
            "description": "Optional item list",
            "title": "Items",
        }

        cleaned = self.provider._clean_schema(schema, defs)

        self.assertEqual(cleaned["type"], "array")
        self.assertEqual(cleaned["items"]["type"], "object")
        self.assertEqual(cleaned["items"]["required"], ["name"])
        self.assertEqual(cleaned["items"]["properties"]["name"], {"type": "string"})
        self.assertEqual(
            cleaned["items"]["properties"]["tags"]["items"],
            {"type": "string"},
        )
        self.assertEqual(cleaned["description"], "Optional item list")
        self.assertIsNone(cleaned["default"])
        self._assert_no_key(cleaned, "title")

    def test_clean_schema_preserves_standard_json_schema_keywords(self):
        schema = {
            "oneOf": [
                {"type": "string", "pattern": "^htb-[a-z]+$"},
                {"type": "integer", "minimum": 1, "maximum": 10},
            ],
            "allOf": [{"examples": ["htb-machine"]}],
            "title": "Dropped",
        }

        cleaned = self.provider._clean_schema(schema, {})

        self.assertEqual(cleaned["oneOf"][0]["pattern"], "^htb-[a-z]+$")
        self.assertEqual(cleaned["oneOf"][1]["minimum"], 1)
        self.assertEqual(cleaned["oneOf"][1]["maximum"], 10)
        self.assertEqual(cleaned["allOf"][0]["examples"], ["htb-machine"])
        self._assert_no_key(cleaned, "title")

    def test_active_tool_schemas_convert_without_defs_or_titles(self):
        for tool in _make_active_tools():
            with self.subTest(tool=tool.name):
                schema = self.provider._convert_to_anthropic_tool_schema(tool)

                self.assertEqual(schema["input_schema"]["type"], "object")
                self.assertIsInstance(schema["input_schema"]["properties"], dict)
                self._assert_no_key(schema["input_schema"], "$defs")
                self._assert_no_key(schema["input_schema"], "definitions")
                self._assert_no_key(schema["input_schema"], "$schema")
                self._assert_no_key(schema["input_schema"], "title")

    @unittest.skipUnless(
        os.environ.get("ANTHROPIC_API_KEY")
        and os.environ.get("ANTHROPIC_SCHEMA_TEST_MODEL"),
        "set ANTHROPIC_API_KEY and ANTHROPIC_SCHEMA_TEST_MODEL to validate "
        "schemas against the real Anthropic API",
    )
    def test_active_tool_schemas_validate_with_anthropic_count_tokens(self):
        api_key = os.environ["ANTHROPIC_API_KEY"]
        model = os.environ["ANTHROPIC_SCHEMA_TEST_MODEL"]
        provider = AnthropicProvider(api_key=api_key, model=model)
        tools = [
            provider._convert_to_anthropic_tool_schema(tool)
            for tool in _make_active_tools()
        ]

        client = anthropic.Anthropic(api_key=api_key)
        client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": "validate tool schemas"}],
            tools=tools,
        )

    def _assert_no_key(self, node, key):
        if isinstance(node, dict):
            self.assertNotIn(key, node)
            for value in node.values():
                self._assert_no_key(value, key)
        elif isinstance(node, list):
            for value in node:
                self._assert_no_key(value, key)


if __name__ == "__main__":
    unittest.main()
