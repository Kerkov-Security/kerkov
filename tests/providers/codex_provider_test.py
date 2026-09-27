"""Tests for Codex provider event parsing."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from kerkov.models.basic_models import Message
from kerkov.providers.codex.provider import (
    CODEX_MODELS_CLIENT_VERSION,
    CodexModelNotAvailableError,
    CodexProvider,
)


class _FakeStreamResponse:
    """Async-context stub replaying a scripted SSE line sequence."""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return self._response


def _sse(event):
    return f"data: {json.dumps(event)}"


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

    def test_fills_arguments_from_delta_buffer_when_item_is_empty(self):
        provider = self._provider()
        tool_calls = []
        tool_call_index_by_id = {}
        raw_output_items_by_id = {}
        argument_buffers = {"fc_789": '{"command":"pwd"}'}

        provider._collect_tool_calls_from_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_789",
                    "call_id": "call_789",
                    "name": "exec",
                    "arguments": "",
                },
            },
            tool_calls,
            tool_call_index_by_id,
            raw_output_items_by_id,
            argument_buffers,
        )

        self.assertEqual(tool_calls[0].parameters, {"command": "pwd"})
        self.assertEqual(
            raw_output_items_by_id["call_789"]["arguments"], '{"command":"pwd"}'
        )

    def test_buffer_recovers_from_truncated_item_arguments(self):
        provider = self._provider()
        tool_calls = []
        raw_output_items_by_id = {}
        argument_buffers = {"fc_1": '{"command":"pwd"}'}

        provider._collect_tool_calls_from_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "exec",
                    "arguments": '{"command":"p',
                },
            },
            tool_calls,
            {},
            raw_output_items_by_id,
            argument_buffers,
        )

        self.assertEqual(tool_calls[0].parameters, {"command": "pwd"})
        self.assertEqual(
            raw_output_items_by_id["call_1"]["arguments"], '{"command":"pwd"}'
        )
        replayed = provider._convert_messages_to_responses_input(
            [
                Message(
                    role="assistant",
                    content="",
                    raw_blocks=list(raw_output_items_by_id.values()),
                )
            ]
        )
        self.assertEqual(replayed[0]["arguments"], '{"command":"pwd"}')

    def test_truncated_item_is_replaced_by_later_complete_item(self):
        provider = self._provider()
        tool_calls = []
        tool_call_index_by_id = {}
        raw_output_items_by_id = {}
        item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "exec",
        }

        for arguments in ('{"command":"p', '{"command":"pwd"}'):
            provider._collect_tool_calls_from_event(
                {
                    "type": "response.output_item.done",
                    "item": {**item, "arguments": arguments},
                },
                tool_calls,
                tool_call_index_by_id,
                raw_output_items_by_id,
            )

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].parameters, {"command": "pwd"})
        self.assertEqual(
            raw_output_items_by_id["call_1"]["arguments"], '{"command":"pwd"}'
        )

    def test_complete_assembles_arguments_from_streamed_deltas(self):
        provider = self._provider()
        call_item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "exec",
            "arguments": "",
        }
        lines = [
            _sse({"type": "response.output_item.added", "item": call_item}),
            _sse(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "delta": '{"command":"nmap ',
                }
            ),
            _sse(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "delta": 'localhost","mode":"foreground"}',
                }
            ),
            _sse(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_1",
                    "arguments": '{"command":"nmap localhost","mode":"foreground"}',
                }
            ),
            # Terminal events echo the arguments empty, the shape that used to
            # surface as an argument-less tool call.
            _sse({"type": "response.output_item.done", "item": dict(call_item)}),
            _sse(
                {
                    "type": "response.completed",
                    "response": {"output": [dict(call_item)]},
                }
            ),
            "data: [DONE]",
        ]
        fake_client = _FakeAsyncClient(_FakeStreamResponse(lines))

        with patch(
            "kerkov.providers.codex.provider.httpx.AsyncClient",
            return_value=fake_client,
        ):
            response = asyncio.run(provider.complete(messages=[]))

        self.assertIsNotNone(response.tool_calls)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].tool_name, "exec")
        self.assertEqual(
            response.tool_calls[0].parameters,
            {"command": "nmap localhost", "mode": "foreground"},
        )
        self.assertEqual(response.tool_calls[0].call_id, "call_1")
        self.assertEqual(
            response.raw_response["output"][0]["arguments"],
            '{"command":"nmap localhost","mode":"foreground"}',
        )

    @patch("kerkov.providers.codex.provider.httpx.Client")
    def test_lists_visible_models_from_authenticated_catalog(self, client_class):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "visibility": "list",
                    "priority": 1,
                },
                {
                    "slug": "gpt-5.6-terra",
                    "visibility": "list",
                    "priority": 2,
                },
                {
                    "slug": "gpt-5.6-luna",
                    "visibility": "list",
                    "priority": 3,
                },
                {
                    "slug": "gpt-5.5",
                    "visibility": "list",
                    "priority": 7,
                },
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

        self.assertEqual(
            models,
            [
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
            ],
        )
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
