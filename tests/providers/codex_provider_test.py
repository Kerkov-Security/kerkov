"""Tests for Codex provider event parsing."""

import unittest
from unittest.mock import MagicMock, patch

from kerkov.providers.codex.provider import (
    CODEX_MODELS_CLIENT_VERSION,
    CodexModelNotAvailableError,
    CodexProvider,
)


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

    @patch("kerkov.providers.codex.provider.httpx.Client")
    def test_lists_visible_models_from_authenticated_catalog(self, client_class):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "models": [
                {
                    "slug": "gpt-5.4",
                    "visibility": "list",
                    "priority": 16,
                },
                {
                    "slug": "gpt-5.4-mini",
                    "visibility": "list",
                    "priority": 23,
                },
                {
                    "slug": "codex-auto-review",
                    "visibility": "hide",
                    "priority": 43,
                },
            ]
        }
        client = client_class.return_value.__enter__.return_value
        client.get.return_value = response
        provider = self._provider()

        models = provider.list_models()

        self.assertEqual(models, ["gpt-5.4", "gpt-5.4-mini"])
        client.get.assert_called_once()
        call = client.get.call_args
        self.assertEqual(
            call.args[0],
            "https://chatgpt.com/backend-api/codex/models",
        )
        self.assertEqual(
            call.kwargs["params"],
            {"client_version": CODEX_MODELS_CLIENT_VERSION},
        )
        self.assertEqual(call.kwargs["headers"]["accept"], "application/json")

    @patch("kerkov.providers.codex.provider.httpx.Client")
    def test_model_catalog_retries_once_after_token_refresh(self, client_class):
        unauthorized = MagicMock()
        unauthorized.status_code = 401
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {
            "models": [{"slug": "gpt-5.4", "visibility": "list"}]
        }
        client = client_class.return_value.__enter__.return_value
        client.get.side_effect = [unauthorized, success]
        provider = self._provider()
        provider.token_manager = MagicMock()
        provider.token_manager.account_id = "test-account"
        provider.token_manager.ensure_valid_token.side_effect = [
            "expired-token",
            "refreshed-token",
        ]

        models = provider.list_models()

        self.assertEqual(models, ["gpt-5.4"])
        self.assertEqual(
            provider.token_manager.ensure_valid_token.call_args_list[0].kwargs,
            {"force_refresh": False},
        )
        self.assertEqual(
            provider.token_manager.ensure_valid_token.call_args_list[1].kwargs,
            {"force_refresh": True},
        )
        self.assertEqual(client.get.call_count, 2)
        self.assertIn(
            "refreshed-token",
            client.get.call_args_list[1].kwargs["headers"]["Authorization"],
        )

    def test_validation_falls_back_to_first_account_model(self):
        provider = CodexProvider(
            model="gpt-5-codex",
            base_url="https://chatgpt.com/backend-api",
            access_token="test-token",
            account_id="test-account",
        )

        with patch.object(
            provider,
            "list_models",
            return_value=["gpt-5.4", "gpt-5.4-mini"],
        ):
            valid = provider.validate_connection()

        self.assertTrue(valid)
        self.assertEqual(provider.model, "gpt-5.4")
        self.assertIn("gpt-5-codex", provider.model_selection_notice)
        self.assertIn("gpt-5.4", provider.model_selection_notice)

    def test_rejects_model_outside_cached_account_catalog(self):
        provider = self._provider()
        provider._available_models = ["gpt-5.4", "gpt-5.4-mini"]

        with self.assertRaises(CodexModelNotAvailableError) as context:
            provider.set_model("gpt-5-codex")

        self.assertIn("/models", str(context.exception))
        self.assertEqual(provider.model, "gpt-5.4")

    def test_models_url_accepts_responses_base(self):
        provider = CodexProvider(
            model="gpt-5.4",
            base_url="https://chatgpt.com/backend-api/codex/responses",
            access_token="test-token",
            account_id="test-account",
        )

        self.assertEqual(
            provider._resolve_models_url(),
            "https://chatgpt.com/backend-api/codex/models",
        )


if __name__ == "__main__":
    unittest.main()
