"""Malformed provider tool arguments are reported, never replaced with `{}`."""

import asyncio
from typing import Any, Callable, Dict, List

import httpx
import pytest

from kerkov.models.basic_models import Message, ToolCall
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.providers.nvidia_provider import NvidiaProvider
from kerkov.providers.ollama_provider import OllamaProvider
from kerkov.providers.openrouter_provider import OpenRouterProvider
from kerkov.providers.tool_arguments import parse_tool_arguments


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_arguments_are_an_empty_object(raw: Any) -> None:
    parsed = parse_tool_arguments(raw)

    assert parsed.parameters == {}
    assert parsed.error is None


@pytest.mark.parametrize("raw", ['{"target": "src"}', {"target": "src"}])
def test_object_arguments_are_returned(raw: Any) -> None:
    parsed = parse_tool_arguments(raw)

    assert parsed.parameters == {"target": "src"}
    assert parsed.error is None


def test_invalid_json_is_reported_with_an_excerpt() -> None:
    parsed = parse_tool_arguments('{"target": "src"')

    assert parsed.parameters == {}
    assert parsed.error == 'arguments were not valid JSON: {"target": "src"'


@pytest.mark.parametrize(
    "raw, kind",
    [("[1, 2]", "array"), ('"src"', "string"), ("3", "number"), ("null", "null")],
)
def test_non_object_json_is_reported(raw: str, kind: str) -> None:
    parsed = parse_tool_arguments(raw)

    assert parsed.parameters == {}
    assert parsed.error == f"arguments must be a JSON object, got {kind}: {raw}"


def test_non_string_arguments_are_reported() -> None:
    parsed = parse_tool_arguments([1, 2])

    assert parsed.error == "arguments must be a JSON object, got list: [1, 2]"


def test_long_arguments_are_excerpted() -> None:
    parsed = parse_tool_arguments("{" + "x" * 500)

    assert parsed.error is not None
    assert parsed.error.endswith("…")
    assert len(parsed.error) < 260


def _chat_completions(provider_factory: Callable[[], Any]):
    def parse(arguments: Any) -> ToolCall:
        calls = provider_factory()._parse_tool_calls(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "sast_scan", "arguments": arguments},
                }
            ]
        )
        return calls[0]

    return parse


def _ollama(monkeypatch: pytest.MonkeyPatch, arguments: Any) -> ToolCall:
    client_type = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "sast_scan", "arguments": arguments}}
                    ],
                },
                "done": True,
            },
        )

    monkeypatch.setattr(
        "kerkov.providers.ollama_provider.httpx.AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    provider = OllamaProvider("http://localhost:11434", "m")
    response = asyncio.run(provider.complete([Message(role="user", content="go")]))
    assert response.tool_calls
    return response.tool_calls[0]


def _codex(arguments: Any) -> ToolCall:
    provider = CodexProvider(
        model="gpt-5.4", base_url="https://chatgpt.com/backend-api", access_token="t"
    )
    call = provider._tool_call_from_item(
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "sast_scan",
            "arguments": arguments,
        }
    )
    assert call is not None
    return call


_PARSERS: Dict[str, Callable[[pytest.MonkeyPatch, Any], ToolCall]] = {
    "deepseek": lambda _mp, args: _chat_completions(
        lambda: DeepSeekProvider(
            api_key="k", base_url="https://api.deepseek.com", model="m"
        )
    )(args),
    "openrouter": lambda _mp, args: _chat_completions(
        lambda: OpenRouterProvider(
            api_key="k", base_url="https://openrouter.ai/api/v1", model="m"
        )
    )(args),
    "nvidia": lambda _mp, args: _chat_completions(
        lambda: NvidiaProvider(
            api_key="k", base_url="https://integrate.api.nvidia.com/v1", model="m"
        )
    )(args),
    "ollama": _ollama,
    "codex": lambda _mp, args: _codex(args),
}
_MALFORMED: List[Any] = ['{"target": "src"', "[1, 2]", '"src"']


@pytest.mark.parametrize("provider", sorted(_PARSERS))
@pytest.mark.parametrize("arguments", _MALFORMED)
def test_provider_reports_malformed_arguments(
    monkeypatch: pytest.MonkeyPatch, provider: str, arguments: Any
) -> None:
    call = _PARSERS[provider](monkeypatch, arguments)

    assert call.parameters == {}
    assert call.argument_error is not None
    assert arguments in call.argument_error


@pytest.mark.parametrize("provider", sorted(_PARSERS))
def test_provider_accepts_empty_arguments(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    call = _PARSERS[provider](monkeypatch, "")

    assert call.parameters == {}
    assert call.argument_error is None


@pytest.mark.parametrize("provider", sorted(_PARSERS))
def test_provider_parses_object_arguments(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    call = _PARSERS[provider](monkeypatch, '{"target": "src"}')

    assert call.parameters == {"target": "src"}
    assert call.argument_error is None
