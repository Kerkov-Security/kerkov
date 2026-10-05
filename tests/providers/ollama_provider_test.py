"""Selected Ollama models must support chat before a provider is installed."""

import json

import httpx
import pytest

from kerkov.providers.ollama_provider import OllamaProvider


def _mock_server(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(
        "kerkov.providers.ollama_provider.httpx.Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "metadata, expected",
    [
        ({"capabilities": ["completion"]}, True),
        ({"capabilities": ["completion", "tools", "vision"]}, True),
        ({"capabilities": ["embedding"]}, False),
        ({"capabilities": []}, False),
        ({"capabilities": None}, False),
        ({"capabilities": "completion"}, False),
        ({"capabilities": {"completion": True}}, False),
        ({"capabilities": ["completion", None]}, False),
        ({"error": "model unavailable"}, False),
        ([], False),
        (None, False),
    ],
)
def test_selected_model_capabilities_are_checked_without_inference(
    monkeypatch, metadata, expected
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/show"
        assert json.loads(request.content) == {"name": "selected-model"}
        return httpx.Response(200, json=metadata)

    _mock_server(monkeypatch, respond)
    provider = OllamaProvider("http://localhost:11434", "selected-model")
    assert provider.validate_model() is expected
    assert len(requests) == 1
    assert provider.model == "selected-model"


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"message": {"role": "assistant", "content": "Hi"}, "done": True}, True),
        (200, {"message": {"role": "assistant", "content": ""}, "done": True}, True),
        (200, {"error": "model does not support chat"}, False),
        (200, {"message": {"role": "assistant"}, "done": False}, False),
        (200, {"message": {"role": "user"}, "done": True}, False),
        (200, {"message": "invalid", "done": True}, False),
        (200, [], False),
        (400, {"error": "model does not support chat"}, False),
        (503, {"error": "model unavailable"}, False),
    ],
)
def test_older_server_requires_minimal_successful_chat_probe(
    monkeypatch, status, payload, expected
):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {}})
        assert request.url.path == "/api/chat"
        assert json.loads(request.content) == {
            "model": "selected-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": False,
            "options": {"num_predict": 1},
        }
        return httpx.Response(status, json=payload)

    _mock_server(monkeypatch, respond)
    provider = OllamaProvider("http://localhost:11434", "selected-model")
    assert provider.validate_model() is expected
    assert [request.url.path for request in requests] == ["/api/show", "/api/chat"]


@pytest.mark.parametrize("failure", ["missing", "timeout", "invalid_json"])
def test_failed_metadata_rejects_without_probing_or_logging_response_body(
    monkeypatch, caplog, failure
):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private response detail", request=request)
        return httpx.Response(
            404 if failure == "missing" else 200, text="private response detail"
        )

    _mock_server(monkeypatch, respond)
    provider = OllamaProvider("http://localhost:11434", "selected-model")
    assert provider.validate_model() is False
    assert len(requests) == 1
    assert "private response detail" not in caplog.text


def test_connection_does_not_require_default_model_to_be_pulled(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        assert request.method == "GET"
        return httpx.Response(200, json={"models": [{"name": "llama3.1"}]})

    _mock_server(monkeypatch, respond)
    provider = OllamaProvider("http://localhost:11434", "gpt-oss")
    assert provider.validate_connection()
    assert seen == ["/api/tags"]
