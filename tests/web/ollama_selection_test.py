"""A listed Ollama embedding model must not replace the active chat provider."""

import httpx
import pytest

from kerkov.models.basic_models import Message
from kerkov.providers.ollama_provider import OllamaProvider
from kerkov.web.runtime import providers
from tests.providers.ollama_provider_test import _mock_server
from tests.web.provider_runtime_test import request, session


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"capabilities": ["embedding"]}, {}])
async def test_embedding_selection_preserves_active_provider_and_conversation(
    session, monkeypatch, metadata
):
    model = "nomic-embed-text:latest"
    candidate = OllamaProvider("http://localhost:11434", "gpt-oss")
    seen = []

    def respond(request):
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": model}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=metadata)
        assert request.url.path == "/api/chat"
        return httpx.Response(400, json={"error": "model does not support chat"})

    _mock_server(monkeypatch, respond)
    monkeypatch.setattr(providers, "create_provider_from_name", lambda *args: candidate)
    session._apply_system_prompt_if_needed()
    session._append_message(Message(role="user", content="prior question"))
    session._append_message(Message(role="assistant", content="prior answer"))
    await session._ensure_compactor()
    previous = session.provider
    history = list(session.conversation_history)
    compactor = session.compactor
    context_limit = compactor.max_tokens
    engagement = session.engagement_store.engagement_id

    event = await request(session, "switch", provider="ollama", model=model)

    assert event["type"] == "provider.selection_error"
    assert event["payload"]["operation"] == "switch"
    assert session.provider is previous
    assert session.conversation_history == history
    assert session.compactor is compactor
    assert compactor.max_tokens == context_limit
    assert session.engagement_store.engagement_id == engagement
    assert not session.providers.switching
    assert not session._lock.locked()
    assert seen == ["/api/tags", "/api/show"] + (
        [] if "capabilities" in metadata else ["/api/chat"]
    )
