"""Behavior shared by the OpenAI-compatible chat-completions providers."""

import asyncio
import json
from typing import Any, Callable, Dict, List
from unittest.mock import patch

import httpx
import pytest

from kerkov.models.basic_models import Message
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.providers.nvidia_provider import NvidiaProvider
from kerkov.providers.openai_compatible import OpenAICompatibleProvider
from kerkov.providers.openrouter_provider import OpenRouterProvider

_FACTORIES: Dict[str, Callable[[], OpenAICompatibleProvider]] = {
    "deepseek": lambda: DeepSeekProvider(
        api_key="k", base_url="https://api.deepseek.com", model="m"
    ),
    "nvidia": lambda: NvidiaProvider(
        api_key="k", base_url="https://integrate.api.nvidia.com/v1", model="m"
    ),
    "openrouter": lambda: OpenRouterProvider(
        api_key="k", base_url="https://openrouter.ai/api/v1", model="a/m"
    ),
}
_LABELS = {"deepseek": "DeepSeek", "nvidia": "NVIDIA NIM", "openrouter": "OpenRouter"}
_PROVIDERS = sorted(_FACTORIES)


def _response_with_call(arguments: Any) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "sast_scan", "arguments": arguments},
                        }
                    ],
                }
            }
        ]
    }


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("arguments", [[], [1], 0, False, "src"])
def test_decoded_non_object_arguments_are_reported(
    provider: str, arguments: Any
) -> None:
    response = _FACTORIES[provider]()._parse_completion_response(
        _response_with_call(arguments)
    )

    assert response.tool_calls
    call = response.tool_calls[0]
    assert call.parameters == {}
    assert call.argument_error is not None


@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("arguments", [None, {"target": "src"}])
def test_decoded_object_or_missing_arguments_are_accepted(
    provider: str, arguments: Any
) -> None:
    response = _FACTORIES[provider]()._parse_completion_response(
        _response_with_call(arguments)
    )

    assert response.tool_calls
    call = response.tool_calls[0]
    assert call.parameters == (arguments or {})
    assert call.argument_error is None


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_replay_keeps_the_rejected_arguments(provider: str) -> None:
    instance = _FACTORIES[provider]()
    response = instance._parse_completion_response(_response_with_call([]))
    history = [
        Message(
            role="assistant",
            content="",
            raw_blocks=response.raw_response["output"],
        )
    ]

    (assistant,) = instance._convert_messages(history)

    assert assistant["tool_calls"][0]["function"]["arguments"] == "[]"


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_tool_result_without_call_id_is_sent_as_user_text(provider: str) -> None:
    converted = _FACTORIES[provider]()._convert_messages(
        [Message(role="tool", content="output")]
    )

    assert converted == [{"role": "user", "content": "Tool result:\noutput"}]


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_usage_breaks_out_cached_prompt_tokens(provider: str) -> None:
    response = _FACTORIES[provider]()._parse_completion_response(
        {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        }
    )

    assert response.usage is not None
    assert response.usage.prompt_tokens == 100
    assert response.usage.total_tokens == 105
    assert response.usage.cache_read_tokens == 60


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_api_errors_name_the_provider(provider: str) -> None:
    error = httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    with pytest.raises(
        ValueError, match=f"^{_LABELS[provider]} API error 401: Invalid API key$"
    ):
        _FACTORIES[provider]()._raise_for_status(error)


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_streaming_errors_read_the_body_before_raising(provider: str) -> None:
    requests: List[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    client_type = httpx.AsyncClient
    with patch(
        "kerkov.providers.openai_compatible.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        with pytest.raises(ValueError, match="429: slow down"):
            asyncio.run(
                _FACTORIES[provider]().complete(
                    [Message(role="user", content="go")], on_delta=lambda _: None
                )
            )

    (request,) = requests
    assert request.url.path.endswith("/chat/completions")
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
