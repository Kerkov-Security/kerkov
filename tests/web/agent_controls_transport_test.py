"""Agent controls remain responsive while the master and other controls run."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.console.consumers import MAX_AGENT_REQUESTS
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.agent_controls_test import _request
from tests.web.agent_panel_test import _report, _spawn
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.mark.asyncio
async def test_control_round_trip_and_approvals_work_during_master_turn_and_stop(
    runtime,
):
    master_started, release_master = asyncio.Event(), asyncio.Event()
    stopping, release_stop = asyncio.Event(), asyncio.Event()

    async def handle_message(session, message):
        async with session._lock:
            master_started.set()
            await release_master.wait()

    async def run(agent):
        agent.events.info("Inspecting target")
        await agent.interrupt.interrupt_requested.wait()
        stopping.set()
        await release_stop.wait()
        return _report(agent)

    with patch.object(WebKerkovSession, "handle_user_message", handle_message):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        approval = None
        try:
            await _until(communicator, "session.ready")
            web = runtime[0]
            agent = _spawn(web, run)
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "master", "message": "Continue"}
            )
            await _until(communicator, "user.message.accepted")
            await asyncio.wait_for(master_started.wait(), 1)
            await communicator.send_json_to(_request(agent, "inspect"))
            output = await _until(communicator, "agent.output")
            assert output["activity"] == "Inspecting target"
            await communicator.send_json_to(
                _request(agent, "message", message="Prioritize auth")
            )
            assert (await _until(communicator, "agent.message_sent"))[
                "request_id"
            ] == "request-message"
            assert agent.inbox.get_nowait() == "Prioritize auth"
            await communicator.send_json_to(_request(agent, "stop"))
            await asyncio.wait_for(stopping.wait(), 1)
            # A real approval resolves while the stop grace period is pending.
            tool = Mock()
            tool.name = "http"
            approval = asyncio.create_task(
                web.approvals.request(tool, {"url": "https://example.com"}, choices={})
            )
            prompt = await _until(communicator, "approval.request")
            await communicator.send_json_to(
                {
                    "type": "approval.response",
                    "approval_id": prompt["approval_id"],
                    "decision": "deny",
                }
            )
            resolved = await _until(communicator, "approval.resolved")
            assert resolved["decision"] == "deny"
            assert (await approval)["decision"] == "deny"
            assert not release_master.is_set()
            assert not agent.task_handle.done()
            release_master.set()
            await _until(communicator, "user.message.complete")
            assert not agent.task_handle.done()
            release_stop.set()
            assert (await _until(communicator, "agent.stopped"))["agent_id"] == agent.id
        finally:
            release_stop.set()
            release_master.set()
            await communicator.disconnect()
            if approval is not None:
                await asyncio.gather(approval, return_exceptions=True)
    assert runtime[0]._closed


@pytest.mark.asyncio
async def test_control_task_count_is_bounded_and_disconnect_cleans_pending_tasks(
    runtime,
):
    started = 0
    settled = 0
    release = asyncio.Event()

    async def handle(session, content):
        nonlocal started, settled
        started += 1
        try:
            await release.wait()
        finally:
            settled += 1

    with patch.object(WebKerkovSession, "handle_agent_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            for index in range(MAX_AGENT_REQUESTS + 1):
                await communicator.send_json_to(
                    {
                        "type": "agent.stop",
                        "request_id": f"stop-{index}",
                        "agent_id": "a1",
                        "agent_run_id": "abcdef012345",
                    }
                )
            rejected = await _until(communicator, "agent.control_error")
            assert rejected["request_id"] == f"stop-{MAX_AGENT_REQUESTS}"
            assert "still running" in rejected["detail"]
            assert started == MAX_AGENT_REQUESTS
            await communicator.send_json_to(
                {
                    "type": "approval.response",
                    "approval_id": "missing",
                    "decision": "deny",
                }
            )
            assert (await _until(communicator, "approval.rejected"))[
                "approval_id"
            ] == "missing"
        finally:
            await communicator.disconnect()
    assert settled == MAX_AGENT_REQUESTS
    assert runtime[0]._closed
