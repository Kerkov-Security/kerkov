"""Smaller model switches prepare history before committing any live state."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage
from kerkov.web.runtime import providers as provider_runtime
from kerkov.web.runtime.provider_history import prepare_history
from kerkov.web.runtime.providers import ProviderSelectionError
from tests.web.provider_runtime_test import candidate, request, session


def long_history(session):
    session._apply_system_prompt_if_needed()
    for index in range(12):
        session._append_message(
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content="x" * 20_000 if index < 10 else "recent exchange",
            )
        )
    session.token_tracker.record_usage(80_000)


@pytest.mark.asyncio
async def test_smaller_model_compacts_with_old_provider_before_first_completion(
    session, candidate, monkeypatch
):
    long_history(session)
    old = session.provider
    old.context_limit = 200_000
    await session._ensure_compactor()
    history = session.conversation_history
    engagement_id = session.engagement_store.engagement_id
    summary = AsyncMock(
        return_value=LLMResponse(
            content="Preserved findings and next steps.",
            model=old.model,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=10),
        )
    )
    monkeypatch.setattr(old, "complete", summary)

    event = await request(session, "switch", provider="gemini", model="new-model")

    assert event["type"] == "provider.changed"
    assert session.provider is candidate
    summary.assert_awaited_once()
    assert not candidate.requests
    assert session.conversation_history is history
    assert any(message.summary_level == 1 for message in history)
    assert sum(len(message.content) for message in history) < 30_000
    assert session.token_tracker.total_tokens == 80_000
    assert session.token_tracker.cumulative_tokens == 80_110
    restored = session.engagement_store.load_engagement(engagement_id)
    assert [m.content for m in restored.messages] == [m.content for m in history]
    await session.handle_user_message("continue")
    assert candidate.requests
    assert any(m.summary_level == 1 for m in candidate.requests[0])
    assert sum(len(m.content) for m in candidate.requests[0]) < 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "empty", "too_large", "uncompactable"])
async def test_history_preparation_failure_preserves_session(
    session, candidate, monkeypatch, failure
):
    long_history(session)
    old = session.provider
    old.context_limit = 200_000
    await session._ensure_compactor()
    if failure == "uncompactable":
        session.conversation_history[:] = [
            Message(role="system", content="instructions"),
            Message(role="user", content="x" * 240_000),
        ]
    history = list(session.conversation_history)
    summary = AsyncMock(
        return_value=LLMResponse(
            content="" if failure == "empty" else "x" * 240_000,
            model=old.model,
        )
    )
    if failure == "error":
        summary.side_effect = RuntimeError("private-key-provider-error")
    monkeypatch.setattr(old, "complete", summary)
    persistence = Mock()
    monkeypatch.setattr(
        session.engagement_store, "override_engagement_messages", persistence
    )

    event = await request(session, "switch", provider="gemini", model="new-model")

    assert event["type"] == "provider.selection_error"
    assert "private-key" not in str(event)
    if failure == "error":
        assert "current provider could not summarize" in event["payload"]["detail"]
    assert session.provider is old
    assert session.compactor.max_tokens == 200_000
    assert session.conversation_history == history
    persistence.assert_not_called()
    assert not candidate.requests
    assert not session.providers.switching


@pytest.mark.asyncio
async def test_effective_output_limit_and_ledger_are_reserved(
    session, candidate, monkeypatch
):
    session.tools = []
    session.conversation_history[:] = [Message(role="system", content="x" * 27_000)]
    candidate.context_limit = 16_000
    monkeypatch.setattr(
        candidate, "get_max_output_tokens", AsyncMock(return_value=8_000)
    )
    event = await request(session, "switch", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert session.provider is not candidate
    session.conversation_history[:] = [Message(role="system", content="instructions")]
    session.findings_ledger = Mock(serialize=AsyncMock(return_value="x" * 60_000))
    with pytest.raises(ProviderSelectionError, match="too large"):
        await prepare_history(session, 16_000, 1_000)


@pytest.mark.asyncio
async def test_already_compacted_history_ignores_stale_token_total(session):
    session.tools = []
    session.conversation_history[:] = [
        Message(role="system", content="instructions"),
        Message(role="assistant", content="Earlier findings", summary_level=1),
        Message(role="user", content="continue"),
    ]
    session.token_tracker.record_usage(80_000)
    assert await prepare_history(session, 16_000, 1_000) is None
    assert session.token_tracker.total_tokens == 80_000


@pytest.mark.asyncio
async def test_trailing_tool_group_stays_intact_during_preparation(
    session, monkeypatch
):
    long_history(session)
    session.tools = []
    call = Message(
        role="assistant",
        content="checking",
        raw_blocks=[
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "exec",
                "input": {},
            }
        ],
    )
    result = Message(role="tool", call_id="call_1", content="result")
    session.conversation_history.extend([call, result])
    monkeypatch.setattr(
        session.provider,
        "complete",
        AsyncMock(return_value=LLMResponse(content="summary", model="old-model")),
    )
    prepared = await prepare_history(session, 16_000, 1_000)
    assert prepared[-2:] == [call, result]


def test_failed_engagement_replacement_keeps_saved_history(tmp_path, monkeypatch):
    from kerkov.core.engagement.engagement_store import EngagementStore

    store = EngagementStore(store_dir=tmp_path)
    store.start_engagement("audit", "old", "standard", "normal")
    store.append_message(Message(role="user", content="original"))
    before = {
        entry.relative_to(tmp_path): entry.read_bytes()
        for entry in tmp_path.rglob("*")
        if entry.is_file()
    }
    monkeypatch.setattr(
        "kerkov.core.engagement.engagement_store.os.replace",
        Mock(side_effect=OSError("disk full")),
    )
    with pytest.raises(OSError):
        store.override_engagement_messages(
            [Message(role="assistant", content="summary")]
        )
    assert {
        entry.relative_to(tmp_path): entry.read_bytes()
        for entry in tmp_path.rglob("*")
        if entry.is_file()
    } == before


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "timeout", "disconnect"])
async def test_abandoned_compaction_never_commits(
    session, candidate, monkeypatch, finish
):
    long_history(session)
    history = list(session.conversation_history)
    old = session.provider
    started = asyncio.Event()
    release = asyncio.Event()

    async def summarize(*args, **kwargs):
        started.set()
        await release.wait()
        return LLMResponse(content="summary", model=old.model)

    monkeypatch.setattr(old, "complete", summarize)
    if finish == "timeout":
        # The summarizer is only reached after prepare_history serializes every
        # tool schema and runs compaction (~150ms here), so the budget must clear
        # that to exercise a timeout mid-summary rather than before one starts.
        monkeypatch.setattr(provider_runtime, "PROVIDER_HISTORY_TIMEOUT_SECONDS", 0.5)
    task = asyncio.create_task(
        session.handle_provider_message(
            {
                "type": "provider.switch",
                "request_id": "switch",
                "provider": "gemini",
                "model": "new-model",
            }
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if finish == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif finish == "disconnect":
            session.close()
            release.set()
            await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
    assert session.provider is old
    assert session.conversation_history == history
    assert not candidate.requests
    assert not session.providers.switching


@pytest.mark.asyncio
async def test_history_preparation_has_its_own_longer_budget(
    session, candidate, monkeypatch
):
    long_history(session)
    old = session.provider

    async def summarize(*args, **kwargs):
        await asyncio.sleep(0.1)
        return LLMResponse(content="summary", model=old.model)

    monkeypatch.setattr(old, "complete", summarize)
    monkeypatch.setattr(provider_runtime, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(provider_runtime, "PROVIDER_HISTORY_TIMEOUT_SECONDS", 1.0)
    event = await request(session, "switch", provider="gemini", model="new-model")
    assert event["type"] == "provider.changed"
    assert session.provider is candidate
