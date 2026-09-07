"""Tests for DeepSeek requests, tool schemas, and conversation replay."""

import asyncio
import json
from typing import Optional
from unittest.mock import patch

import httpx
import pytest
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, Field

from kerkov.core.config_manager import ConfigManager
from kerkov.core.conversation_engine import extract_raw_blocks
from kerkov.core.credentials import (
    DEEPSEEK_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.models.basic_models import Message
from kerkov.providers import (
    MissingCredentialError,
    create_provider_from_name,
    list_providers,
)
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


def _provider() -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key="test-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-v4-flash",
    )


class _NestedParams(BaseModel):
    title: str = Field(min_length=1, max_length=20)
    values: list[str] = Field(min_length=1, max_length=3)


class _FakeParams(ToolParams):
    query: str = Field(description="Thing to inspect", min_length=1, max_length=50)
    nested: Optional[_NestedParams] = None


class _FakeTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("fake_tool", "fake tool")

    def get_params_class(self):
        return _FakeParams

    async def run(self, toolParams: _FakeParams) -> ToolResult:
        return ToolResult(success=True, output=toolParams.query)

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def _tool_call() -> dict:
    return {
        "id": "call_123",
        "type": "function",
        "function": {"name": "fake_tool", "arguments": '{"query":"README.md"}'},
    }


@pytest.mark.parametrize("options", [{}, {"temperature": 0.0, "max_tokens": 256}])
def test_complete_sends_openai_request_and_parses_usage(options) -> None:
    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["Content-Type"] == "application/json"
        assert "HTTP-Referer" not in request.headers
        assert json.loads(request.content) == {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
            **options,
        }
        assert request.extensions["timeout"]["read"] == 60
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": "Hello!"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 8,
                    "prompt_cache_miss_tokens": 2,
                },
            },
        )

    client_type = httpx.AsyncClient
    with patch(
        "kerkov.providers.deepseek_provider.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        response = asyncio.run(
            _provider().complete([Message(role="user", content="Hello")], **options)
        )
    assert response.content == "Hello!"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 15
    assert response.tool_calls is None


def test_tool_conversation_preserves_reasoning_across_resume_and_next_turn() -> None:
    requests = []
    responses = [
        {
            "content": None,
            "reasoning_content": "Inspect file.",
            "tool_calls": [_tool_call()],
        },
        {"content": "Read the file.", "reasoning_content": "File inspected."},
        {"content": "Done.", "reasoning_content": "Answer follow-up."},
    ]

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "thinking" not in payload
        assert payload["tool_choice"] == "auto"
        assert payload["tools"][0]["function"]["name"] == "fake_tool"
        assert "strict" not in payload["tools"][0]["function"]
        return httpx.Response(
            200, json={"choices": [{"message": responses[len(requests) - 1]}]}
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    provider = _provider()
    messages = [Message(role="user", content="Read the file")]

    async def run_conversation():
        for turn in range(3):
            response = await provider.complete(messages, tools=[_FakeTool()])
            if turn == 0:
                assert response.tool_calls[0].parameters == {"query": "README.md"}
                assert response.tool_calls[0].call_id == "call_123"
            # Persisted engagements retain raw blocks through JSON serialization.
            raw_blocks = json.loads(
                json.dumps(extract_raw_blocks(response.raw_response))
            )
            messages.append(
                Message(
                    role="assistant", content=response.content, raw_blocks=raw_blocks
                )
            )
            if turn == 0:
                messages.append(
                    Message(role="tool", content="File contents", call_id="call_123")
                )
            elif turn == 1:
                messages.append(Message(role="user", content="Finish"))

    with patch(
        "kerkov.providers.deepseek_provider.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(**kwargs, transport=transport),
    ):
        asyncio.run(run_conversation())

    assert requests[1]["messages"][1] == {
        "role": "assistant",
        "content": "",
        "reasoning_content": "Inspect file.",
        "tool_calls": [_tool_call()],
    }
    assert requests[1]["messages"][2] == {
        "role": "tool",
        "content": "File contents",
        "tool_call_id": "call_123",
    }
    assert requests[2]["messages"][1]["reasoning_content"] == "Inspect file."
    assert requests[2]["messages"][3]["reasoning_content"] == "File inspected."


def test_imported_history_disables_thinking_without_mutating_saved_reasoning() -> None:
    raw = {
        "role": "assistant",
        "content": "Prior answer",
        "reasoning_content": "Saved reasoning",
    }
    messages = [
        Message(role="assistant", content="Imported answer"),
        Message(role="assistant", content="Prior answer", raw_blocks=[raw]),
        Message(role="user", content="Continue"),
    ]

    def respond(request):
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert all(
            "reasoning_content" not in message for message in payload["messages"]
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done"}}]})

    client_type = httpx.AsyncClient
    with patch(
        "kerkov.providers.deepseek_provider.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        asyncio.run(_provider().complete(messages, tools=[_FakeTool()]))
    assert raw["reasoning_content"] == "Saved reasoning"


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {"type": "text", "text": "Checking"},
            {
                "type": "tool_use",
                "id": "call_123",
                "name": "fake_tool",
                "input": {"query": "README.md"},
            },
        ],
        [
            TextBlock(type="text", text="Checking"),
            ToolUseBlock(
                type="tool_use",
                id="call_123",
                name="fake_tool",
                input={"query": "README.md"},
            ),
        ],
        [
            {"type": "output_text", "text": "Checking"},
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "fake_tool",
                "arguments": '{"query":"README.md"}',
            },
        ],
        [
            {"type": "text", "text": "Checking"},
            {
                "type": "function_call",
                "id": "call_123",
                "name": "fake_tool",
                "args": {"query": "README.md"},
            },
        ],
    ],
)
def test_cross_provider_tool_blocks_convert_to_chat_messages(blocks) -> None:
    converted = _provider()._convert_messages(
        [
            Message(role="assistant", content="", raw_blocks=blocks),
            Message(role="tool", content="contents", call_id="call_123"),
        ]
    )
    assert converted[0]["content"] == "Checking"
    call = converted[0]["tool_calls"][0]
    assert call["id"] == "call_123"
    assert call["function"]["name"] == "fake_tool"
    assert json.loads(call["function"]["arguments"]) == {"query": "README.md"}
    assert converted[1]["tool_call_id"] == "call_123"


def test_plain_messages_and_responses_output_text() -> None:
    converted = _provider()._convert_messages(
        [
            Message(role="system", content="System"),
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content="",
                raw_blocks=[
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Prior answer."}],
                    }
                ],
            ),
            Message(role="tool", content="Orphan result"),
        ]
    )
    assert converted == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Prior answer."},
        {"role": "user", "content": "Tool result:\nOrphan result"},
    ]


def test_tool_schema_inlines_refs_and_removes_constraints_without_dropping_fields() -> (
    None
):
    schema = _provider()._convert_tool_schema(_FakeTool())
    parameters = schema["function"]["parameters"]
    assert schema["type"] == "function"
    assert parameters["properties"]["query"] == {
        "type": "string",
        "description": "Thing to inspect",
    }
    nested = parameters["properties"]["nested"]["anyOf"][0]
    assert nested["properties"]["title"] == {"type": "string"}
    assert nested["properties"]["values"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert nested["required"] == ["title", "values"]
    encoded = json.dumps(parameters)
    for key in (
        "$defs",
        "$ref",
        "$schema",
        "definitions",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    ):
        assert key not in encoded
    assert "title" not in parameters


def test_generated_tool_id_matches_replay() -> None:
    call = _tool_call()
    del call["id"]
    response = _provider()._parse_completion_response(
        {"choices": [{"message": {"tool_calls": [call]}}]}
    )
    assert response.tool_calls[0].call_id
    assert (
        response.tool_calls[0].call_id
        == response.raw_response["output"][0]["tool_calls"][0]["id"]
    )


def test_token_limits_use_existing_configuration_fallbacks() -> None:
    assert asyncio.run(_provider().get_max_context_tokens()) is None
    assert asyncio.run(_provider().get_max_output_tokens()) is None


@pytest.mark.parametrize("status", [200, 401])
def test_model_listing_and_connection_validation(status) -> None:
    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/models"
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            status,
            json={"data": [{"id": "deepseek-v4-flash"}, {}, {"id": "deepseek-v4-pro"}]},
        )

    client_type = httpx.Client
    with patch(
        "kerkov.providers.deepseek_provider.httpx.Client",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ) as clients:
        assert _provider().list_models() == (
            ["deepseek-v4-flash", "deepseek-v4-pro"] if status == 200 else []
        )
        assert clients.call_args.kwargs["timeout"] == 10
        assert _provider().validate_connection() is (status == 200)
        assert clients.call_args.kwargs["timeout"] == 60


@pytest.mark.parametrize(
    "response, detail",
    [
        (httpx.Response(500, content=b"upstream unavailable"), "upstream unavailable"),
        (
            httpx.Response(401, json={"error": {"message": "Invalid API key"}}),
            "Invalid API key",
        ),
        (httpx.Response(402, content=b""), "Insufficient balance"),
    ],
)
def test_api_errors_are_actionable(response, detail) -> None:
    with pytest.raises(
        ValueError, match=f"DeepSeek API error {response.status_code}: .*{detail}"
    ):
        DeepSeekProvider._raise_for_status(response)


def test_factory_supports_deepseek() -> None:
    config = ConfigManager(
        DEEPSEEK_API_KEY="test-key",
        DEEPSEEK_BASE_URL="https://deepseek.example/v1/",
        LLM_PROVIDER="deepseek",
        DEFAULT_DEEPSEEK_MODEL="deepseek-v4-pro",
        MAX_TIMEOUT_SECONDS=42,
    )
    provider = create_provider_from_name(config, "deepseek")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.api_key == "test-key"
    assert provider.base_url == "https://deepseek.example/v1"
    assert provider.model == "deepseek-v4-pro"
    assert provider.timeout_seconds == 42
    assert "deepseek" in list_providers()


def test_factory_resolves_runtime_credential_and_reports_missing_key() -> None:
    config = ConfigManager()
    with pytest.raises(MissingCredentialError) as exc:
        create_provider_from_name(config, "deepseek")
    assert exc.value.spec == DEEPSEEK_API_KEY
    credentials = CredentialManager(
        persistent_store=MemorySecretStore(), environment={}
    )
    credentials.set_session(DEEPSEEK_API_KEY, "runtime-key")
    provider = create_provider_from_name(config, "deepseek", credentials)
    assert provider.api_key == "runtime-key"
