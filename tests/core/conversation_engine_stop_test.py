"""A queued Stop wins before scheduled provider or tool work begins."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.cli.cancellable_task import EventInterrupt
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.conversation_engine import ConversationEngine
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.tools.base_tool import ToolResult
from tests.core.conversation_engine_test import (
    _EchoTool,
    _make_provider,
    _make_session,
    _RecordingSink,
)
from tests.core.streaming_test import _StreamRecordingSink


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


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool_response", [False, True])
def test_stop_discards_provider_response_that_wins_interrupt_race(
    streaming, tool_response
):
    async def run():
        interrupt = EventInterrupt()
        sink = _StreamRecordingSink() if streaming else _RecordingSink()
        provider = _make_provider([])
        tool = _EchoTool()
        tool.run = AsyncMock(wraps=tool.run)
        saved = Mock()
        history = [Message(role="user", content="Keep this conversation")]

        async def complete(**kwargs):
            if kwargs["on_delta"] is not None:
                kwargs["on_delta"]("Already ")
                kwargs["on_delta"]("visible")
            # Do not yield after Stop: the provider task and watcher finish
            # together, and run_cancellable returns the provider's result.
            interrupt.interrupt_requested.set()
            return LLMResponse(
                content="Late final answer that was never streamed",
                model="test",
                usage=TokenUsage(
                    prompt_tokens=100, completion_tokens=20, total_tokens=120
                ),
                tool_calls=(
                    [ToolCall(tool_name="echo", parameters={}, call_id="late-call")]
                    if tool_response
                    else None
                ),
            )

        provider.complete.side_effect = complete
        session = _make_session(
            provider,
            history=list(history),
            tools=[tool],
            output=sink,
            interrupt=interrupt,
            interrupt_ends_turn=True,
        )
        session.engagement_store = saved
        session.token_tracker = TokenTracker()

        result = await ConversationEngine().run_loop(session)

        assert result is None
        assert session.token_tracker.cumulative_tokens == 120
        provider.complete.assert_awaited_once()
        tool.run.assert_not_awaited()
        assert interrupt.interrupt_requested.is_set()
        assert session.history[:1] == history
        if streaming:
            assert sink.deltas == ["Already ", "visible"]
            assert sink.begins == sink.ends == 1
            assert sink.finals == []
            assert len(session.history) == 2
            partial = session.history[-1]
            assert partial.role == "assistant"
            assert partial.content == "Already visible"
            assert partial.partial is True
            assert partial.raw_blocks is None
            saved.append_message.assert_called_once_with(partial)
        else:
            assert sink.final_calls == []
            assert session.history == history
            saved.append_message.assert_not_called()
        saved.update_summary.assert_not_called()

    asyncio.run(run())


def test_cli_provider_completion_still_wins_interrupt_race():
    async def run():
        interrupt = EventInterrupt()
        provider = _make_provider([])
        response = LLMResponse(content="Completed CLI answer", model="test")

        async def complete(**kwargs):
            interrupt.interrupt_requested.set()
            return response

        provider.complete.side_effect = complete
        sink = _RecordingSink()
        session = _make_session(provider, output=sink, interrupt=interrupt)

        result = await ConversationEngine().run_loop(session)

        assert result is response
        assert sink.final_calls == [response.content]
        assert session.history[-1].content == response.content
        assert session.history[-1].partial is False

    asyncio.run(run())


def test_stop_preserves_racing_exempt_report_and_skips_other_calls():
    async def run():
        interrupt = EventInterrupt()
        provider = _make_provider([])
        report = _EchoTool()
        report.run = AsyncMock(wraps=report.run)

        async def complete(**kwargs):
            interrupt.interrupt_requested.set()
            return LLMResponse(
                content="Found evidence",
                model="test",
                tool_calls=[
                    ToolCall(tool_name="other", parameters={}, call_id="skipped"),
                    ToolCall(
                        tool_name="echo",
                        parameters={"text": "findings"},
                        call_id="report",
                    ),
                ],
                usage=TokenUsage(
                    prompt_tokens=100, completion_tokens=20, total_tokens=120
                ),
            )

        provider.complete.side_effect = complete
        session = _make_session(
            provider,
            tools=[report],
            interrupt=interrupt,
            interrupt_ends_turn=True,
            stop_exempt_tools=frozenset({"echo"}),
        )
        session.token_tracker = TokenTracker()
        await ConversationEngine().run_loop(session)
        report.run.assert_awaited_once()
        assert session.token_tracker.cumulative_tokens == 120
        results = {m.call_id: m.content for m in session.history if m.role == "tool"}
        assert "skipped" in results and "report" in results
        assert "findings" in results["report"]

    asyncio.run(run())
