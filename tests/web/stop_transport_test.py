"""Main-run Stop is correlated, cooperative, and waits for turn cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.console.consumers import MAX_MESSAGE_ID_CHARS, ConsoleConsumer
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.inventory_transport_test import _communicator, _until, runtime


def _consumer(handle_message):
    """Exercise admission before the event loop can start the reserved task."""
    consumer = ConsoleConsumer()
    consumer.bus = WebEventBus()
    consumer.session = SimpleNamespace(
        handle_user_message=handle_message,
        approvals=Mock(),
    )
    consumer._message_task = None
    consumer._active_message_id = None
    consumer._message_interrupt = None
    consumer._provider_mutation_task = None
    return consumer


@pytest.mark.asyncio
async def test_stop_before_message_task_starts_and_duplicate_stop_are_preserved():
    observed = []

    async def handle(message, *, interrupt):
        observed.append((message, interrupt.interrupt_requested.is_set()))

    consumer = _consumer(handle)
    await consumer.receive_json(
        {"type": "user.message", "message_id": "first", "message": "continue"}
    )
    assert observed == []
    for _ in range(2):
        await consumer.receive_json(
            {"type": "user.message.stop", "message_id": "first"}
        )
    consumer.session.approvals.cancel_all.assert_called_once_with(
        reason="Run stopped by user before approval."
    )
    await consumer._message_task
    assert observed == [("continue", True)]
    events = consumer.bus.drain_nowait()
    assert [event["type"] for event in events] == [
        "user.message.accepted",
        "user.message.stopping",
        "user.message.stopping",
        "user.message.complete",
    ]
    assert events[-1]["payload"] == {"message_id": "first", "stopped": True}
    assert consumer._active_message_id is None
    assert consumer._message_interrupt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, "", 123, [], {}, "x" * 129, "stale"])
async def test_invalid_and_stale_stops_leave_current_run_untouched(message_id):
    observed = []

    async def handle(message, *, interrupt):
        observed.append(interrupt.interrupt_requested.is_set())

    consumer = _consumer(handle)
    await consumer.receive_json(
        {"type": "user.message", "message_id": "current", "message": "continue"}
    )
    await consumer.receive_json({"type": "user.message.stop", "message_id": message_id})
    consumer.session.approvals.cancel_all.assert_not_called()
    await consumer._message_task
    assert observed == [False]
    events = consumer.bus.drain_nowait()
    assert events[1]["type"] == "user.message.stop_rejected"
    expected_id = (
        message_id
        if isinstance(message_id, str) and 0 < len(message_id) <= MAX_MESSAGE_ID_CHARS
        else ""
    )
    assert events[1]["payload"]["message_id"] == expected_id
    assert events[-1]["payload"]["stopped"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["clear", "/clear", "/mode", "/mode audit"])
async def test_web_commands_reject_stop(command):
    async def handle(message, *, interrupt):
        assert not interrupt.interrupt_requested.is_set()

    consumer = _consumer(handle)
    await consumer.receive_json(
        {"type": "user.message", "message_id": "command", "message": command}
    )
    await consumer.receive_json({"type": "user.message.stop", "message_id": "command"})
    await consumer._message_task
    consumer.session.approvals.cancel_all.assert_not_called()
    events = consumer.bus.drain_nowait()
    assert events[1]["type"] == "user.message.stop_rejected"
    assert "command" in events[1]["payload"]["detail"]
    assert events[-1]["payload"]["stopped"] is False


@pytest.mark.asyncio
async def test_stop_without_active_run_and_delayed_stop_cannot_stop_next_message():
    observed = []

    async def handle(message, *, interrupt):
        observed.append(interrupt.interrupt_requested.is_set())

    consumer = _consumer(handle)
    await consumer.receive_json({"type": "user.message.stop", "message_id": "first"})
    for message_id in ["first", "second"]:
        await consumer.receive_json(
            {"type": "user.message", "message_id": message_id, "message": "continue"}
        )
        if message_id == "second":
            await consumer.receive_json(
                {"type": "user.message.stop", "message_id": "first"}
            )
        await consumer._message_task
    await consumer.receive_json({"type": "user.message.stop", "message_id": "second"})
    assert observed == [False, False]
    consumer.session.approvals.cancel_all.assert_not_called()
    events = consumer.bus.drain_nowait()
    assert len([e for e in events if e["type"] == "user.message.stop_rejected"]) == 3
    assert len([e for e in events if e["type"] == "user.message.complete"]) == 2


@pytest.mark.asyncio
async def test_stop_settles_approval_and_holds_admission_until_cleanup(runtime):
    cleaning, release_cleanup = asyncio.Event(), asyncio.Event()
    approved = []

    async def handle(session, message, *, interrupt):
        if message == "next":
            assert not interrupt.interrupt_requested.is_set()
            return
        tool = Mock()
        tool.name = "http"
        approved.append(
            await session.approvals.request(
                tool, {"url": "https://example.com"}, choices={}
            )
        )
        assert interrupt.interrupt_requested.is_set()
        cleaning.set()
        await release_cleanup.wait()

    with patch.object(WebKerkovSession, "handle_user_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "first", "message": "run"}
            )
            await _until(communicator, "user.message.accepted")
            prompt = await _until(communicator, "approval.request")
            await communicator.send_json_to(
                {"type": "user.message.stop", "message_id": "first"}
            )
            assert (await _until(communicator, "user.message.stopping")) == {
                "message_id": "first"
            }
            await asyncio.wait_for(cleaning.wait(), 1)
            assert approved[0]["decision"] == "deny"
            assert "stopped" in approved[0]["user_message"]
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "too-soon", "message": "next"}
            )
            assert (await _until(communicator, "user.message.rejected"))[
                "message_id"
            ] == "too-soon"
            # Approval and agent requests remain responsive during main cleanup.
            await communicator.send_json_to(
                {
                    "type": "approval.response",
                    "approval_id": prompt["approval_id"],
                    "decision": "approve_once",
                }
            )
            await _until(communicator, "approval.rejected")
            await communicator.send_json_to(
                {
                    "type": "agent.stop",
                    "request_id": "agent-independent",
                    "agent_id": "missing",
                    "agent_run_id": "abcdef012345",
                }
            )
            assert (await _until(communicator, "agent.control_error"))[
                "request_id"
            ] == "agent-independent"
            await communicator.send_json_to(
                {"type": "user.message.stop", "message_id": "first"}
            )
            await _until(communicator, "user.message.stopping")
            release_cleanup.set()
            assert await _until(communicator, "user.message.complete") == {
                "message_id": "first",
                "stopped": True,
            }
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "next", "message": "next"}
            )
            await _until(communicator, "user.message.accepted")
            assert await _until(communicator, "user.message.complete") == {
                "message_id": "next",
                "stopped": False,
            }
        finally:
            release_cleanup.set()
            await communicator.disconnect()
