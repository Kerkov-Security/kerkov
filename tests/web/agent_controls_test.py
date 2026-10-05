"""Agent output, inbox and cancellation controls preserve session ownership."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.core.conversation_engine import ConversationEngine, LoopSession
from kerkov.models.basic_models import Message
from kerkov.web.runtime.agents import (
    MAX_ACTIVITY_CHARS,
    MAX_INSTRUCTION_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_PENDING_INSTRUCTIONS,
)
from tests.web.agent_panel_test import _report, _session, _spawn


def _request(agent: AgentSession, operation: str, **values) -> dict:
    return {
        "type": f"agent.{operation}",
        "request_id": f"request-{operation}",
        "agent_id": agent.id,
        "agent_run_id": agent.agent_run_id,
        **values,
    }


def _reply(web, event_type: str) -> dict:
    return next(
        event["payload"]
        for event in web.bus.drain_nowait()
        if event["type"] == event_type
    )


def _agent(web, **changes) -> AgentSession:
    agent = AgentSession(
        id="a1",
        agent_run_id="012345abcdef",
        profile="recon",
        task="Inspect the target",
        **changes,
    )
    web.agent_supervisor._sessions[agent.id] = agent
    return agent


@pytest.mark.asyncio
async def test_inspect_filters_internal_content_and_never_consumes_report(tmp_path):
    web = _session(tmp_path)
    agent = _agent(web, state=AgentState.DONE)
    agent.history = [
        Message(role="system", content="private system prompt"),
        Message(role="user", content="Review the target"),
        Message(
            role="assistant",
            content="Reviewing routes",
            raw_blocks={"private-provider-block": "secret"},
        ),
        Message(role="tool", content="HTTP 200 evidence", call_id="private-call-id"),
    ]
    agent.events.info("Finished the review")
    agent.report = _report(agent)
    try:
        await web.handle_agent_message(_request(agent, "inspect"))
        reply = _reply(web, "agent.output")
        assert reply["output"] == (
            "User:\nReview the target\n\nAssistant:\nReviewing routes"
            "\n\nTool:\nHTTP 200 evidence"
        )
        assert reply["activity"] == "Finished the review"
        assert reply["truncated"] is False
        assert "private" not in str(reply)
        assert agent.report_delivered is False
        assert web.agent_supervisor.drain_finished_reports() == [(agent, agent.report)]
    finally:
        web.close()


@pytest.mark.asyncio
async def test_inspect_returns_bounded_newest_output_and_activity(tmp_path):
    web = _session(tmp_path)
    agent = _agent(web)
    agent.history = [
        Message(role="assistant", content="old output"),
        Message(role="tool", content="x" * (MAX_OUTPUT_CHARS * 2) + "latest result"),
    ]
    agent.events.buffer = ["old activity", "y" * MAX_ACTIVITY_CHARS + "latest event"]
    try:
        await web.handle_agent_message(_request(agent, "inspect"))
        reply = _reply(web, "agent.output")
        assert len(reply["output"]) <= MAX_OUTPUT_CHARS
        assert reply["output"].endswith("latest result")
        assert "old output" not in reply["output"]
        assert len(reply["activity"]) <= MAX_ACTIVITY_CHARS
        assert reply["activity"].endswith("latest event")
        assert reply["truncated"] is True
    finally:
        web.close()


@pytest.mark.asyncio
async def test_messages_use_existing_inbox_and_persist_at_loop_boundary(tmp_path):
    web = _session(tmp_path)
    agent = _agent(web)
    sibling = AgentSession(
        id="a2", agent_run_id="abcdef012345", profile="recon", task="Other target"
    )
    web.agent_supervisor._sessions[sibling.id] = sibling
    store = Mock()
    loop = LoopSession(
        provider=Mock(),
        history=agent.history,
        tools=[],
        approval=Mock(),
        output=agent.events,
        max_iterations=5,
        inbox=agent.inbox,
        engagement_store=store,
    )
    try:
        async with web._lock:
            await web.handle_agent_message(
                _request(agent, "message", message="  Prioritize authentication  ")
            )
        reply = _reply(web, "agent.message_sent")
        assert "Queued" in reply["detail"]
        assert not agent.history
        assert sibling.inbox.empty()
        assert web.conversation_history == []
        ConversationEngine()._drain_inbox(loop)
        assert agent.inbox.empty()
        assert agent.history[-1].role == "user"
        assert agent.history[-1].content.endswith("Prioritize authentication")
        store.append_message.assert_called_once_with(agent.history[-1])
        # A retry after delivery must not duplicate the operator instruction.
        await web.handle_agent_message(
            _request(agent, "message", message="Prioritize authentication")
        )
        assert "already accepted" in _reply(web, "agent.control_error")["detail"]
        assert agent.inbox.empty()
    finally:
        web.close()


@pytest.mark.asyncio
async def test_message_rejects_done_task_before_lifecycle_callback(tmp_path):
    web = _session(tmp_path)
    agent = _agent(web)
    agent.task_handle = asyncio.create_task(asyncio.sleep(0))
    await agent.task_handle
    try:
        # An abnormal task exit can precede the supervisor's done callback.
        assert agent.is_active
        await web.handle_agent_message(_request(agent, "message", message="Too late"))
        assert "finished" in _reply(web, "agent.control_error")["detail"]
        assert agent.inbox.empty()
    finally:
        web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", [None, 42, "", "  ", "x" * (MAX_INSTRUCTION_CHARS + 1)]
)
async def test_invalid_instructions_never_enqueue(tmp_path, message):
    web = _session(tmp_path)
    agent = _agent(web)
    try:
        await web.handle_agent_message(_request(agent, "message", message=message))
        assert _reply(web, "agent.control_error")["operation"] == "message"
        assert agent.inbox.empty()
        assert not agent.events.buffer
    finally:
        web.close()


@pytest.mark.asyncio
async def test_pending_instructions_are_bounded_and_stopping_agents_reject_input(
    tmp_path,
):
    web = _session(tmp_path)
    agent = _agent(web)
    try:
        for index in range(MAX_PENDING_INSTRUCTIONS + 1):
            await web.handle_agent_message(
                _request(
                    agent, "message", request_id=f"message-{index}", message="Continue"
                )
            )
        assert agent.inbox.qsize() == MAX_PENDING_INSTRUCTIONS
        assert "too many" in _reply(web, "agent.control_error")["detail"]
        agent.cancel_requested = True
        await web.handle_agent_message(
            _request(agent, "message", request_id="stopping", message="Continue")
        )
        assert "stopping" in _reply(web, "agent.control_error")["detail"]
        agent.state = AgentState.DONE
        await web.handle_agent_message(
            _request(agent, "message", request_id="done", message="Continue")
        )
        assert "finished" in _reply(web, "agent.control_error")["detail"]
    finally:
        web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "bad\nrequest"},
        {"request_id": []},
        {"agent_id": "a999"},
        {"agent_id": "other-agent-name"},
        {"agent_id": {}},
        {"agent_run_id": "fedcba543210"},
        {"agent_run_id": "<script>"},
    ],
)
async def test_invalid_and_unknown_identities_never_expose_or_mutate(tmp_path, changes):
    web = _session(tmp_path)
    agent = _agent(web)
    agent.history = [Message(role="assistant", content="private target content")]
    try:
        for operation in ("inspect", "message", "stop"):
            await web.handle_agent_message(
                _request(agent, operation, message="Instruction", **changes)
            )
            reply = _reply(web, "agent.control_error")
            assert "private" not in str(reply)
            assert "<script>" not in str(reply)
            assert agent.inbox.empty()
            assert not agent.cancel_requested
    finally:
        web.close()


@pytest.mark.asyncio
async def test_cross_session_and_reused_ids_cannot_control_another_run(tmp_path):
    first = _session(tmp_path / "first")
    second = _session(tmp_path / "second")
    old = _agent(first)
    target = _agent(second)
    target.agent_run_id = "abcdef123456"
    try:
        await second.handle_agent_message(_request(old, "message", message="Wrong run"))
        assert _reply(second, "agent.control_error")
        assert target.inbox.empty()
        await first._reset_agents()
        new = _agent(first)
        new.agent_run_id = "fedcba654321"
        await first.handle_agent_message(
            _request(old, "message", message="Old request")
        )
        assert _reply(first, "agent.control_error")
        assert new.inbox.empty()
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_stop_keeps_siblings_running_and_partial_report_deliverable(tmp_path):
    web = _session(tmp_path)
    started = asyncio.Event()

    async def run(agent):
        started.set()
        await agent.interrupt.interrupt_requested.wait()
        report = _report(agent, findings="Partial evidence")
        report.status = "partial"
        return report

    async def sibling_run(agent):
        await asyncio.Event().wait()

    agent = _spawn(web, run)
    sibling = _spawn(web, sibling_run)
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with web._lock:
            await web.handle_agent_message(_request(agent, "stop"))
        reply = _reply(web, "agent.stopped")
        assert reply["agent_run_id"] == agent.agent_run_id
        assert agent.state == AgentState.CANCELLED
        assert agent.cancel_requested is True
        assert agent.report.status == "partial"
        assert agent.report_delivered is False
        assert not sibling.task_handle.done()
        assert not sibling.cancel_requested
    finally:
        web.close()
        await asyncio.gather(
            agent.task_handle, sibling.task_handle, return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["reset", "close"])
async def test_stop_drops_late_completion_after_retirement(tmp_path, retirement):
    web = _session(tmp_path)
    stopping, release = asyncio.Event(), asyncio.Event()

    async def run(agent):
        await agent.interrupt.interrupt_requested.wait()
        stopping.set()
        await release.wait()
        return _report(agent)

    agent = _spawn(web, run)
    stop = asyncio.create_task(web.handle_agent_message(_request(agent, "stop")))
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        if retirement == "reset":
            reset = asyncio.create_task(web._reset_agents())
            await asyncio.sleep(0)
            # The old supervisor is already retired while cancellation settles.
            await web.handle_agent_message(
                _request(agent, "message", message="Too late", request_id="late")
            )
            assert agent.inbox.empty()
            release.set()
            await reset
        else:
            web.close()
        await stop
        events = web.bus.drain_nowait()
        assert not any(event["type"] == "agent.stopped" for event in events)
        if retirement == "close":
            await web.handle_agent_message(_request(agent, "inspect"))
            assert web.bus.drain_nowait() == []
    finally:
        release.set()
        web.close()
        await asyncio.gather(agent.task_handle, stop, return_exceptions=True)
