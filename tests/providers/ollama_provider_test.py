"""Selected Ollama models must support chat before a provider is installed."""

import json

import httpx
import pytest

from kerkov.providers.model_catalog import ModelCatalogError
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


def _refused(request):
    try:
        raise ConnectionRefusedError(10061, "actively refused")
    except ConnectionRefusedError as exc:
        raise httpx.ConnectError(f"refused {request.url}") from exc


def _timed_out(request):
    raise httpx.ConnectTimeout(f"timed out {request.url}")


def _unreachable(request):
    raise httpx.ConnectError(f"name resolution failed {request.url}")


def test_model_catalog_lists_named_models(monkeypatch):
    def respond(request):
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "llama3"}, {}, {"name": ""}, "x"]}
        )

    _mock_server(monkeypatch, respond)
    assert OllamaProvider("http://localhost:11434", "m").list_models() == ["llama3"]


def test_empty_model_catalog_is_not_an_error(monkeypatch):
    _mock_server(monkeypatch, lambda request: httpx.Response(200, json={"models": []}))
    assert OllamaProvider("http://localhost:11434", "m").list_models() == []


@pytest.mark.parametrize(
    "handler, message",
    [
        (
            _refused,
            "Couldn't reach Ollama at http://localhost:11434 (connection refused).",
        ),
        (_timed_out, "Couldn't reach Ollama at http://localhost:11434 (timed out)."),
        (
            _unreachable,
            "Couldn't reach Ollama at http://localhost:11434 (connection failed).",
        ),
        (
            lambda request: httpx.Response(404, text="404 page body"),
            "Ollama at http://localhost:11434 returned HTTP 404 Not Found "
            "for its model list.",
        ),
        (
            lambda request: httpx.Response(200, text="<html>proxy login</html>"),
            "Ollama at http://localhost:11434 returned an unreadable model list.",
        ),
        (
            lambda request: httpx.Response(200, json={"error": "nope"}),
            "Ollama at http://localhost:11434 returned an unreadable model list.",
        ),
    ],
)
def test_model_catalog_failures_name_their_cause(monkeypatch, handler, message):
    _mock_server(monkeypatch, handler)
    with pytest.raises(ModelCatalogError) as caught:
        OllamaProvider("http://localhost:11434", "m").list_models()
    assert str(caught.value) == message


def test_model_catalog_failure_hides_endpoint_userinfo_path_and_query(monkeypatch):
    _mock_server(monkeypatch, _refused)
    provider = OllamaProvider(
        "https://operator:hunter2@ollama.internal:8443/proxy-token?key=abc", "m"
    )
    with pytest.raises(ModelCatalogError) as caught:
        provider.list_models()
    assert str(caught.value) == (
        "Couldn't reach Ollama at https://ollama.internal:8443 (connection refused)."
    )
    assert caught.value.__cause__ is None
