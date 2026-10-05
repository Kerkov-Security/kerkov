"""Tests for the NVIDIA NIM provider's OpenAI-compatible message handling."""

import json
from unittest.mock import patch

import httpx
import pytest

from kerkov.models.basic_models import Message
from kerkov.providers.nvidia_provider import NvidiaProvider


def _provider() -> NvidiaProvider:
    return NvidiaProvider(
        api_key="test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        model="z-ai/glm-5.2",
    )


def test_tool_calls_are_parsed() -> None:
    provider = _provider()
    calls = provider._parse_tool_calls(
        [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                },
            }
        ]
    )

    assert len(calls) == 1
    assert calls[0].tool_name == "read_file"
    assert calls[0].parameters == {"path": "README.md"}
    assert calls[0].call_id == "call_123"


def test_tool_call_and_result_round_trip() -> None:
    provider = _provider()
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
                {"role": "assistant", "content": None, "tool_calls": raw_tool_calls}
            ],
        ),
        Message(role="tool", content="contents", call_id="call_123"),
    ]

    converted = provider._convert_messages(messages)

    assert converted[1]["tool_calls"] == raw_tool_calls
    assert converted[2] == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "call_123",
    }


def test_plain_messages_are_openai_compatible() -> None:
    converted = _provider()._convert_messages(
        [
            Message(role="system", content="Be concise"),
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi"),
        ]
    )

    assert converted == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]


@pytest.mark.parametrize("status", [200, 401, 403, 503])
def test_validation_checks_model_access_when_public_catalog_succeeds(status, caplog):
    provider = _provider()
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": provider.model}]})
        return httpx.Response(
            status,
            json=(
                {"choices": [{"message": {"content": ""}}]}
                if status == 200
                else {"error": {"message": "bad test-key"}}
            ),
        )

    client_type = httpx.Client
    with patch(
        "kerkov.providers.nvidia_provider.httpx.Client",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        assert provider.list_models() == [provider.model]
        assert provider.validate_model() is (status == 200)
    check = requests[-1]
    assert check.method == "POST"
    assert check.url.path == "/v1/chat/completions"
    assert check.headers["Authorization"] == "Bearer test-key"
    assert json.loads(check.content) == {
        "model": provider.model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 1,
        "stream": False,
    }
    assert "test-key" not in caplog.text


@pytest.mark.parametrize("payload", [{}, {"error": "failed"}, {"choices": []}])
def test_validation_rejects_non_completion_success_body(payload):
    provider = _provider()
    client = httpx.Client(
        base_url=provider.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    with patch("kerkov.providers.nvidia_provider.httpx.Client", return_value=client):
        assert provider.validate_model() is False


@pytest.mark.parametrize("status", [200, 401, 429, 503])
def test_connection_uses_catalog_without_billable_inference(status, caplog):
    provider = _provider()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, json={"data": []})

    client = httpx.Client(
        base_url=provider.base_url, transport=httpx.MockTransport(respond)
    )
    with patch("kerkov.providers.nvidia_provider.httpx.Client", return_value=client):
        assert provider.validate_connection() is (status == 200)
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/models"
    if status != 200:
        assert str(status) in caplog.text
