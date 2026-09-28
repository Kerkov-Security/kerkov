"""Agent panel events and engagement isolation for browser-owned runtimes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSpawnSpec
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.agents.models import AgentRunReport
from kerkov.core.config_manager import ConfigManager
from kerkov.models.basic_models import Message
from kerkov.tools.base_tool import ToolResult
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession


def _session(tmp_path: Path) -> WebKerkovSession:
    return WebKerkovSession(
        WebEventBus(),
        config=ConfigManager(
            GOOGLE_API_KEY="test",
            EXECUTION_ENABLED=False,
            WORKSPACE_DIR=str(tmp_path / "workspaces"),
            USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
            HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
        ),
    )


def _spawn(
    web: WebKerkovSession,
    run: Callable[[AgentSession], Awaitable[AgentRunReport]],
) -> AgentSession:
    def build_runner(agent: AgentSession, _workspace: Optional[Path]) -> AgentRunner:
        runner = MagicMock(spec=AgentRunner)
        runner.effective_max_iterations.return_value = 7

        async def run_agent() -> AgentRunReport:
            return await run(agent)

        runner.run = AsyncMock(side_effect=run_agent)
        return runner

    return web.agent_supervisor.spawn(
        [
            AgentSpawnSpec(
                profile="recon",
                task="Check the scoped target",
                name="target-check",
                build_runner=build_runner,
            )
        ],
        parent_depth=0,
    )[0]


def _report(
    agent: AgentSession, *, findings: str = "Found one issue"
) -> AgentRunReport:
    return AgentRunReport(
        status="completed",
        findings=findings,
        next_steps="Verify the evidence",
        agent_type=agent.profile,
        agent_run_id=agent.agent_run_id,
        engagement_id="private-engagement-id",
        tokens_used=42,
    )


def _updates(web: WebKerkovSession) -> list[dict]:
    return [
        event["payload"]["agent"]
        for event in web.bus.drain_nowait()
        if event["type"] == "agent.updated"
    ]


@pytest.mark.asyncio
async def test_agent_panel_streams_lifecycle_tool_and_report_without_raw_output(
    tmp_path: Path,
) -> None:
    web = _session(tmp_path)
    running = asyncio.Event()
    finish = asyncio.Event()

    async def run(agent: AgentSession) -> AgentRunReport:
        agent.history.append(Message(role="system", content="private system prompt"))
        agent.events.iteration_start(1)
        agent.events.tool_start("http", {"url": "private tool argument"})
        running.set()
        await finish.wait()
        agent.events.tool_succeeded("http", ToolResult(success=True, output="secret"))
        return _report(agent)

    try:
        agent = _spawn(web, run)
        await asyncio.wait_for(running.wait(), timeout=1)
        snapshots = _updates(web)
        assert [item["state"] for item in snapshots[:2]] == ["queued", "running"]
        assert snapshots[-1] == {
            "id": agent.id,
            "name": "target-check",
            "task": "Check the scoped target",
            "profile": "recon",
            "state": "running",
            "current_tool": "http",
            "iteration": 1,
            "max_iterations": 7,
            "report": None,
        }

        agent.events.info("private buffered output")
        agent.events.info("more private output")
        assert _updates(web) == []

        finish.set()
        await asyncio.wait_for(agent.task_handle, timeout=1)
        snapshots = _updates(web)
        assert snapshots[0]["current_tool"] is None
        assert snapshots[-1]["state"] == "done"
        assert snapshots[-1]["report"] == {
            "status": "completed",
            "findings": "Found one issue",
            "next_steps": "Verify the evidence",
            "tokens_used": 42,
        }
        assert "private" not in str(snapshots)
        assert "secret" not in str(snapshots)

        web._deliver_agent_reports()
        assert len(web.conversation_history) == 1
        assert "Found one issue" in web.conversation_history[0].content
        web._deliver_agent_reports()
        assert len(web.conversation_history) == 1
    finally:
        web.close()


@pytest.mark.asyncio
async def test_close_unsubscribes_and_ignores_late_agent_callbacks(
    tmp_path: Path,
) -> None:
    web = _session(tmp_path)

    async def run(agent: AgentSession) -> AgentRunReport:
        await asyncio.Event().wait()
        return _report(agent)

    agent = _spawn(web, run)
    listener = web._agent_listener
    assert listener in web.agent_supervisor._subscribers
    web.bus.drain_nowait()

    web.close()
    await asyncio.gather(agent.task_handle, return_exceptions=True)
    listener(agent, "state")

    assert listener not in web.agent_supervisor._subscribers
    assert agent.state == AgentState.CANCELLED
    assert _updates(web) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/clear", "clear", "/mode recon"])
async def test_conversation_reset_settles_agents_and_retires_old_callbacks_and_reports(
    tmp_path: Path, command: str
) -> None:
    web = _session(tmp_path)
    started = asyncio.Event()

    async def run(agent: AgentSession) -> AgentRunReport:
        started.set()
        await agent.interrupt.interrupt_requested.wait()
        agent.events.info("old output during cancellation")
        return _report(agent, findings="old engagement finding")

    try:
        old_agent = _spawn(web, run)
        await asyncio.wait_for(started.wait(), timeout=1)
        old_supervisor = web.agent_supervisor
        old_listener = web._agent_listener
        web.bus.drain_nowait()

        await web.handle_user_message(command)

        assert old_agent.task_handle.done()
        assert old_agent.state == AgentState.CANCELLED
        assert old_agent.report_delivered is True
        assert old_supervisor.drain_finished_reports() == []
        assert old_listener not in old_supervisor._subscribers
        assert web.agent_supervisor is not old_supervisor
        assert web.agent_supervisor.list() == []
        assert web.agent_supervisor.get(old_agent.id) is None
        assert web.agent_registry.total_spawned == 1
        for tool in web.tools:
            if isinstance(tool, (SpawnAgentTool, AgentWaitTool)):
                assert tool._supervisor is web.agent_supervisor

        events = web.bus.drain_nowait()
        assert events[0]["type"] == "inventory.snapshot"
        assert events[0]["payload"]["entries"] == []
        assert events[1]["type"] == "agents.reset"
        assert events[1]["payload"] == {}
        assert not any(event["type"] == "agent.updated" for event in events)
        old_listener(old_agent, "state")
        old_agent.notify("output")
        web._deliver_agent_reports()
        assert web.conversation_history == []
        assert _updates(web) == []

        async def new_run(agent: AgentSession) -> AgentRunReport:
            return _report(agent, findings="new engagement finding")

        new_agent = _spawn(web, new_run)
        await asyncio.wait_for(new_agent.task_handle, timeout=1)
        assert _updates(web)[-1]["report"]["findings"] == "new engagement finding"
        web._deliver_agent_reports()
        assert len(web.conversation_history) == 1
        assert "new engagement finding" in web.conversation_history[0].content
        assert "old engagement finding" not in web.conversation_history[0].content
    finally:
        web.close()


@pytest.mark.asyncio
async def test_mode_info_and_invalid_mode_leave_current_agents_visible(
    tmp_path: Path,
) -> None:
    web = _session(tmp_path)

    async def run(agent: AgentSession) -> AgentRunReport:
        return _report(agent)

    try:
        agent = _spawn(web, run)
        await asyncio.wait_for(agent.task_handle, timeout=1)
        supervisor = web.agent_supervisor
        web.bus.drain_nowait()

        await web.handle_user_message("/mode")
        await web.handle_user_message("/mode audit")
        await web.handle_user_message("/mode missing")

        assert web.agent_supervisor is supervisor
        assert web.agent_supervisor.get(agent.id) is agent
        assert not any(
            event["type"] == "agents.reset" for event in web.bus.drain_nowait()
        )
    finally:
        web.close()


@pytest.mark.asyncio
async def test_agent_events_are_isolated_between_browser_sessions(
    tmp_path: Path,
) -> None:
    first = _session(tmp_path / "first")
    second = _session(tmp_path / "second")

    async def run(agent: AgentSession) -> AgentRunReport:
        return _report(agent)

    try:
        agent = _spawn(first, run)
        await asyncio.wait_for(agent.task_handle, timeout=1)

        assert _updates(first)[-1]["state"] == "done"
        assert second.agent_supervisor.list() == []
        assert _updates(second) == []
        await first.handle_user_message("/clear")
        assert second.bus.drain_nowait() == []
    finally:
        first.close()
        second.close()
