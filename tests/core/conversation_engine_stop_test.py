"""A queued Stop wins before scheduled provider or tool work begins."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.cli.cancellable_task import EventInterrupt
from kerkov.core.conversation_engine import ConversationEngine
from kerkov.models.basic_models import LLMResponse, Message, ToolCall
from kerkov.tools.base_tool import ToolResult
from tests.core.conversation_engine_test import (
    _EchoTool,
    _make_provider,
    _make_session,
    _RecordingSink,
)


class _QueuedStopSink(_RecordingSink):
    def __init__(self, interrupt, phase):
        super().__init__()
        self.interrupt = interrupt
        self.phase = phase

    @contextmanager
    def status(self, label):
        if self.phase in label:
            # Simulate a Stop already queued to run before the newly scheduled
            # coroutine, after all synchronous checks in the parent task.
            asyncio.get_running_loop().call_soon(self.interrupt.interrupt_requested.set)
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ends_turn,exempt,expected_runs",
    [
        (True, False, []),
        (False, False, ["first", "second"]),
        (True, True, ["first", "second"]),
    ],
)
async def test_queued_stop_prevents_instant_tool_effects_without_changing_cli_or_exempt_tools(
    ends_turn, exempt, expected_runs
):
    ran = []

    class InstantTool(_EchoTool):
        async def run(self, params):
            ran.append(params.text)
            return ToolResult(success=True, output=params.text)

    interrupt = EventInterrupt()
    provider = _make_provider(
        [
            LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name="echo", parameters={"text": value}, call_id=call_id
                    )
                    for value, call_id in [("first", "c1"), ("second", "c2")]
                ],
            ),
            LLMResponse(content="done", model="test"),
        ]
    )
    session = _make_session(
        provider,
        tools=[InstantTool()],
        output=_QueuedStopSink(interrupt, "Running echo"),
        interrupt=interrupt,
        interrupt_ends_turn=ends_turn,
        stop_exempt_tools=frozenset({"echo"}) if exempt else frozenset(),
    )

    await ConversationEngine().run_loop(session)

    assert ran == expected_runs
    assert [
        message.call_id for message in session.history if message.role == "tool"
    ] == [
        "c1",
        "c2",
    ]
    if ends_turn:
        assert provider.complete.await_count == 1
    else:
        assert provider.complete.await_count == 2
    if not expected_runs:
        results = [
            message.content for message in session.history if message.role == "tool"
        ]
        assert "interrupted" in results[0]
        assert "Skipped" in results[1]


@pytest.mark.asyncio
async def test_queued_stop_prevents_scheduled_provider_request():
    interrupt = EventInterrupt()
    provider = _make_provider(
        [LLMResponse(content="must not be requested", model="test")]
    )
    session = _make_session(
        provider,
        output=_QueuedStopSink(interrupt, "Thinking"),
        interrupt=interrupt,
        interrupt_ends_turn=True,
    )

    await ConversationEngine().run_loop(session)

    provider.complete.assert_not_awaited()
    assert session.history == []
    assert interrupt.interrupt_requested.is_set()


@pytest.mark.asyncio
async def test_queued_stop_prevents_compaction_provider_call_and_history_commit(
    monkeypatch,
):
    interrupt = EventInterrupt()
    provider = _make_provider([LLMResponse(content="late summary", model="test")])
    compactor = Mock()
    compactor.should_compact.return_value = True
    compactor.summary_reserve = 100

    async def compact(history, *, summarize_fn):
        summary = await summarize_fn("Summarize the prior conversation")
        return [Message(role="assistant", content=summary)]

    compactor.compact = AsyncMock(side_effect=compact)
    saved = Mock()
    history = [Message(role="user", content="Preserve the original conversation")]
    session = _make_session(
        provider,
        history=list(history),
        compactor=compactor,
        interrupt=interrupt,
        interrupt_ends_turn=True,
    )
    session.engagement_store = saved
    engine = ConversationEngine()
    await_interruptible = engine._await_interruptible

    async def queue_stop_before_compaction(session, coroutine):
        # The outer compaction check already passed. Stop is first in the
        # event-loop queue; the new compaction coroutine is scheduled after it.
        asyncio.get_running_loop().call_soon(interrupt.interrupt_requested.set)
        return await await_interruptible(session, coroutine)

    monkeypatch.setattr(engine, "_await_interruptible", queue_stop_before_compaction)
    await engine._check_compaction(session)

    assert interrupt.interrupt_requested.is_set()
    compactor.compact.assert_awaited_once()
    provider.complete.assert_not_awaited()
    saved.override_engagement_messages.assert_not_called()
    assert session.history == history
