"""Tests for the OpenRouter provider's OpenAI-compatible message handling."""

import asyncio
import json
from typing import Optional
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.config_manager import ConfigManager
from kerkov.models.basic_models import Message
from kerkov.providers import create_provider_from_name, list_providers
from kerkov.providers.openrouter_provider import OpenRouterProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


def _provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4",
        http_referer="https://kerkov.example",
        app_title="Kerkov Test",
    )


class _NestedParams(BaseModel):
    title: str = Field(min_length=1)
    value: str


class _FakeParams(ToolParams):
    title: str = Field(description="Finding title")
    query: str = Field(description="Thing to inspect")
    nested: Optional[_NestedParams] = Field(None, description="Optional nested input")


class _FakeTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("fake_tool", "fake tool")

    def get_params_class(self):
        return _FakeParams

    async def run(self, toolParams: _FakeParams) -> ToolResult:
        return ToolResult(success=True, output=toolParams.query)

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def test_headers_include_auth_and_optional_attribution() -> None:
    headers = _provider()._headers

    assert headers["Authorization"] == "Bearer test-key"
    assert headers["HTTP-Referer"] == "https://kerkov.example"
    assert headers["X-OpenRouter-Title"] == "Kerkov Test"


def test_tool_calls_are_parsed_and_preserved_for_replay() -> None:
    response = _provider()._parse_completion_response(
        {
            "model": "openai/gpt-4",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "exec",
                                    "arguments": '{"command":"pwd"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
    )

    assert response.content == "Checking."
    assert response.tool_calls[0].tool_name == "exec"
    assert response.tool_calls[0].parameters == {"command": "pwd"}
    assert response.tool_calls[0].call_id == "call_123"
    assert response.usage.total_tokens == 15
    assert response.raw_response["output"][0]["tool_calls"][0]["id"] == "call_123"
    json.dumps(response.raw_response)


def test_missing_tool_call_id_uses_same_generated_id_for_raw_replay() -> None:
    response = _provider()._parse_completion_response(
        {
            "model": "openai/gpt-4",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "exec",
                                    "arguments": '{"command":"pwd"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
    )

    assert response.tool_calls[0].call_id
    assert (
        response.tool_calls[0].call_id
        == response.raw_response["output"][0]["tool_calls"][0]["id"]
    )


def test_tool_call_and_result_round_trip() -> None:
    raw_tool_calls = [
        {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
    ]
    messages = [
        Message(role="user", content="Read the README"),
        Message(
            role="assistant",
            content="",
            raw_blocks=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": raw_tool_calls,
                }
            ],
        ),
        Message(role="tool", content="contents", call_id="call_123"),
    ]

    converted = _provider()._convert_messages(messages)

    assert converted[1]["tool_calls"] == raw_tool_calls
    assert converted[2] == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "call_123",
    }


def test_cross_provider_function_blocks_convert_to_openai_tool_calls() -> None:
    converted = _provider()._convert_messages(
        [
            Message(
                role="assistant",
                content="",
                raw_blocks=[
                    {"type": "text", "text": "I will call a tool."},
                    {
                        "type": "function_call",
                        "id": "call_abc",
                        "name": "process",
                        "args": {"action": "paste"},
                    },
                ],
            )
        ]
    )

    assert converted[0]["content"] == "I will call a tool."
    assert converted[0]["tool_calls"][0]["id"] == "call_abc"
    assert converted[0]["tool_calls"][0]["function"]["name"] == "process"
    arguments = converted[0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"action": "paste"}


def test_responses_output_text_blocks_convert_to_plain_chat_content() -> None:
    converted = _provider()._convert_messages(
        [
            Message(
                role="assistant",
                content="",
                raw_blocks=[
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Prior answer."}],
                    }
                ],
            )
        ]
    )

    assert converted == [{"role": "assistant", "content": "Prior answer."}]


def test_tool_schema_resolves_defs_and_drops_schema_titles() -> None:
    schema = _provider()._convert_tool_schema(_FakeTool())
    parameters = schema["function"]["parameters"]

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "fake_tool"
    assert "$defs" not in parameters
    assert "title" not in parameters


def test_tool_schema_preserves_title_parameters_at_each_level() -> None:
    parameters = _provider()._convert_tool_schema(_FakeTool())["function"]["parameters"]

    assert parameters["properties"]["title"] == {
        "type": "string",
        "description": "Finding title",
    }
    assert set(parameters["required"]) == {"title", "query"}
    nested = parameters["properties"]["nested"]["anyOf"][0]
    assert nested["properties"]["title"] == {"type": "string", "minLength": 1}
    assert set(nested["required"]) == {"title", "value"}
    assert "title" not in nested


def test_model_context_limit_is_cached_without_defaulting_max_output() -> None:
    provider = _provider()

    async def fake_fetch():
        return {
            "context_length": 32000,
            "top_provider": {"max_completion_tokens": "4096"},
        }

    provider._fetch_model_metadata = fake_fetch

    asyncio.run(provider._populate_model_token_caches())

    assert provider._context_window_cache["openai/gpt-4"] == 32000
    assert provider._max_output_cache["openai/gpt-4"] is None
    assert asyncio.run(provider.get_max_output_tokens()) is None


def test_list_models_uses_short_http_timeout() -> None:
    seen = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "openai/gpt-4"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, path):
            assert path == "/models"
            return FakeResponse()

    provider = OpenRouterProvider(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4",
        timeout_seconds=180,
    )

    with patch("kerkov.providers.openrouter_provider.httpx.Client", FakeClient):
        assert provider.list_models() == ["openai/gpt-4"]

    assert seen["timeout"] == 10


def test_raise_for_status_falls_back_to_response_text_for_non_json() -> None:
    response = httpx.Response(500, content=b"upstream unavailable")

    with pytest.raises(ValueError) as exc:
        OpenRouterProvider._raise_for_status(response)

    assert "upstream unavailable" in str(exc.value)


def test_factory_supports_openrouter() -> None:
    config = ConfigManager(
        OPENROUTER_API_KEY="test-key",
        LLM_PROVIDER="openrouter",
        DEFAULT_OPENROUTER_MODEL="openrouter/auto",
        MAX_TIMEOUT_SECONDS=42,
    )

    provider = create_provider_from_name(config, "openrouter")

    assert isinstance(provider, OpenRouterProvider)
    assert provider.model == "openrouter/auto"
    assert provider.timeout_seconds == 42
    assert "openrouter" in list_providers()
