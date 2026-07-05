"""Tests for Codex provider event parsing."""

import unittest

from kerkov.providers.codex.provider import CodexProvider


class CodexProviderTest(unittest.TestCase):
    """Focused tests for streamed Responses API tool-call extraction."""

    def _provider(self) -> CodexProvider:
        return CodexProvider(
            model="gpt-5.4",
            base_url="https://chatgpt.com/backend-api",
            access_token="test-token",
            account_id="test-account",
        )

    def test_collects_tool_call_from_output_item_event(self):
        provider = self._provider()
        tool_calls = []
        tool_call_index_by_id = {}
        raw_output_items_by_id = {}

        provider._collect_tool_calls_from_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_123",
                    "name": "exec",
                    "arguments": '{"command":"pwd"}',
                },
            },
            tool_calls,
            tool_call_index_by_id,
            raw_output_items_by_id,
        )

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].tool_name, "exec")
        self.assertEqual(tool_calls[0].parameters, {"command": "pwd"})
        self.assertEqual(tool_calls[0].call_id, "fc_123")

    def test_dedupes_same_tool_call_seen_in_item_and_completed_response(self):
        provider = self._provider()
        tool_calls = []
        tool_call_index_by_id = {}
        raw_output_items_by_id = {}
        event = {
            "type": "response.completed",
            "item": {
                "type": "function_call",
                "call_id": "call_123",
                "name": "exec",
                "arguments": '{"command":"nmap localhost"}',
            },
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "exec",
                        "arguments": '{"command":"nmap localhost"}',
                    }
                ]
            },
        }

        provider._collect_tool_calls_from_event(
            event,
            tool_calls,
            tool_call_index_by_id,
            raw_output_items_by_id,
        )

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].call_id, "call_123")

    def test_updates_tool_call_when_later_event_has_arguments(self):
        provider = self._provider()
        tool_calls = []
        tool_call_index_by_id = {}
        raw_output_items_by_id = {}

        provider._collect_tool_calls_from_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "call_id": "call_456",
                    "name": "exec",
                    "arguments": "",
                },
            },
            tool_calls,
            tool_call_index_by_id,
            raw_output_items_by_id,
        )
        provider._collect_tool_calls_from_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_456",
                    "name": "exec",
                    "arguments": '{"command":"pwd"}',
                },
            },
            tool_calls,
            tool_call_index_by_id,
            raw_output_items_by_id,
        )

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].parameters, {"command": "pwd"})
        self.assertEqual(
            raw_output_items_by_id["call_456"]["arguments"], '{"command":"pwd"}'
        )


if __name__ == "__main__":
    unittest.main()
