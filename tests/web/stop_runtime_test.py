"""Stopping a browser turn preserves its engagement and unrelated agent work."""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kerkov.agents.agent_session import AgentState
from kerkov.context.context_compactor import ContextCompactor
from kerkov.core.cancellable_task import EventInterrupt
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.core.session.approval_mode_manager import ApprovalMode
from kerkov.core.session.approval_rules import ApprovalRulesEngine
from kerkov.core.session.verbosity_manager import Verbosity
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.exec_tool import ExecTool
from kerkov.web.runtime import session as session_module
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.agent_panel_test import _report, _spawn
from tests.web.web_runtime_test import (
    _collect_until,
    _config,
    _EchoTool,
    _FakeProvider,
)

STOP_REASON = "Run stopped by user before approval."


class _BlockingProvider(_FakeProvider):
    def __init__(self, *, phase: str = "complete") -> None:
        super().__init__([LLMResponse(content="Continued safely", model="fake-model")])
        self.phase = phase
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.block = True
        self.completions = 0

    async def _wait(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.completions += 1
        if self.block and (
            self.phase == "complete"
            or (self.phase == "summary" and self.completions == 2)
        ):
            await self._wait()
        return await super().complete(*args, **kwargs)

    async def get_max_context_tokens(self):
        if self.block and self.phase == "context":
            await self._wait()
        return await super().get_max_context_tokens()

    async def get_max_output_tokens(self):
        if self.block and self.phase == "output":
            await self._wait()
        return await super().get_max_output_tokens()


class _BlockingTool(_EchoTool):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[str] = []

    async def run(self, toolParams) -> ToolResult:
        self.calls.append(toolParams.text)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _batch() -> LLMResponse:
    return LLMResponse(
        content="Checking three resources",
        model="fake-model",
        tool_calls=[
            ToolCall(tool_name="echo", parameters={"text": name}, call_id=name)
            for name in ("first", "second", "third")
        ],
    )


def _stop(web: WebKerkovSession, interrupt: EventInterrupt) -> None:
    interrupt.interrupt_requested.set()
    web.approvals.cancel_all(reason=STOP_REASON)


async def _cleanup(web: WebKerkovSession, task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await web.agent_supervisor.cancel_all()
    web.close()


def _assert_balanced_results(web: WebKerkovSession) -> None:
    results = [m for m in web.conversation_history if m.role == "tool"]
    assert [m.call_id for m in results] == ["first", "second", "third"]
    assert all(
        any(word in m.content.lower() for word in ("stop", "interrupt", "skip"))
        for m in results
    )
    saved = web.engagement_store.load_engagement(web.engagement_store.engagement_id)
    assert saved is not None
    persisted = [m for m in saved.messages if m.role == "tool"]
    assert [(m.call_id, m.content) for m in persisted] == [
        (m.call_id, m.content) for m in results
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["complete", "context", "output"])
async def test_stop_cancels_provider_work_and_next_turn_keeps_session(
    tmp_path: Path, phase: str
) -> None:
    provider = _BlockingProvider(phase=phase)
    config = _config(tmp_path).model_copy(update={"kb_enabled": True})
    web = WebKerkovSession(WebEventBus(), config=config, provider=provider)
    await web.initialize()
    web.approval_mode_manager.set(ApprovalMode.STRICT)
    web.verbosity_manager.set(Verbosity.GUIDED)
    await web.handle_inventory_message(
        {
            "type": "inventory.save",
            "request_id": "save-before-stop",
            "context_id": web.inventory.context_id,
            "section": "assets",
            "resource": {"kind": "domain", "value": "example.test"},
        }
    )
    assert web.findings_ledger is not None
    before = await web.findings_ledger.list(section=LedgerSection.ASSETS)
    assert len(before) == 1
    engagement_id = web.engagement_store.engagement_id
    workspace = web.workspace_path
    knowledge_base = web.knowledge_base
    inventory_context = web.inventory.context_id
    interrupt = EventInterrupt()
    task = asyncio.create_task(
        web.handle_user_message("Start work", interrupt=interrupt)
    )
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=2)
        assert provider.cancelled.is_set()
        assert interrupt.interrupt_requested.is_set()
        assert provider.completions == (1 if phase == "complete" else 0)
        assert web.provider is provider
        assert web.engagement_store.engagement_id == engagement_id
        assert web.workspace_path == workspace
        assert web.knowledge_base is knowledge_base
        assert web.inventory.context_id == inventory_context
        assert web.approval_mode_manager.current == ApprovalMode.STRICT
        assert web.verbosity_manager.current == Verbosity.GUIDED
        assert await web.findings_ledger.list(section=LedgerSection.ASSETS) == before
        assert not web._closed
        assert not web._lock.locked()
        assert not any(e["type"] == "error" for e in web.bus.drain_nowait())

        provider.block = False
        await asyncio.wait_for(
            web.handle_user_message("Continue", interrupt=EventInterrupt()), timeout=2
        )
        assert web.engagement_store.engagement_id == engagement_id
        assert web.conversation_history[-1].content == "Continued safely"
        assert [m.content for m in web.conversation_history if m.role == "user"] == [
            "Start work",
            "Continue",
        ]
    finally:
        await _cleanup(web, task)


@pytest.mark.asyncio
async def test_stop_during_compaction_cancels_summary_without_replacing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _BlockingProvider(phase="summary")
    provider.responses = [LLMResponse(content="Completed answer", model="fake-model")]
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path), provider=provider)
    await web.initialize()
    monkeypatch.setattr(ContextCompactor, "should_compact", lambda self: True)

    async def compact(self, history, *, summarize_fn):
        await summarize_fn("Summarize the previous conversation")
        pytest.fail("A stopped summarizer must not commit a replacement history")

    monkeypatch.setattr(ContextCompactor, "compact", compact)
    interrupt = EventInterrupt()
    task = asyncio.create_task(web.handle_user_message("Answer", interrupt=interrupt))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        history = list(web.conversation_history)
        engagement_id = web.engagement_store.engagement_id
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=2)
        assert provider.cancelled.is_set()
        assert provider.completions == 2
        assert web.conversation_history == history
        assert history[-1].content == "Completed answer"
        saved = web.engagement_store.load_engagement(engagement_id)
        assert saved is not None
        assert saved.messages == history
        assert not web._lock.locked()
        assert not any(e["type"] == "error" for e in web.bus.drain_nowait())
    finally:
        await _cleanup(web, task)


@pytest.mark.asyncio
async def test_queued_stop_prevents_startup_provider_metadata_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider([])
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path), provider=provider)
    await web.initialize()
    interrupt = EventInterrupt()
    run_cancellable = session_module.run_cancellable

    async def queue_stop_before_metadata(coroutine, event):
        asyncio.get_running_loop().call_soon(event.set)
        return await run_cancellable(coroutine, event)

    monkeypatch.setattr(session_module, "run_cancellable", queue_stop_before_metadata)
    try:
        await web.handle_user_message("Start work", interrupt=interrupt)
        assert interrupt.interrupt_requested.is_set()
        provider.get_max_context_tokens.assert_not_awaited()
        assert provider.requests == []
        assert web.conversation_history[-1].content == "Start work"
        assert not web._lock.locked()
        assert not any(e["type"] == "error" for e in web.bus.drain_nowait())
    finally:
        web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_first", [False, True])
async def test_stop_pending_approval_never_runs_side_effect_or_remaining_calls(
    tmp_path: Path, approve_first: bool
) -> None:
    provider = _FakeProvider([_batch()])
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path), provider=provider)
    tool = _EchoTool()
    tool.run = AsyncMock(wraps=tool.run)
    web.tools = [tool]
    web.approval_mode_manager.set(ApprovalMode.STRICT)
    web.approval_strategy._rules_engine = ApprovalRulesEngine()
    await web.initialize()
    interrupt = EventInterrupt()
    task = asyncio.create_task(
        web.handle_user_message("Run checks", interrupt=interrupt)
    )
    try:
        events = await _collect_until(web.bus, "approval.request", timeout=2)
        approval_id = events[-1]["payload"]["approval_id"]
        if approve_first:
            assert web.resolve_approval(approval_id, "approve")
        # Deliberately do not yield between approval and stop: cancellation must
        # win before even a tool whose coroutine returns immediately can run.
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=2)
        tool.run.assert_not_awaited()
        assert len(provider.requests) == 1
        _assert_balanced_results(web)
        assert not web.resolve_approval(approval_id, "approve")
        assert not any(
            e["type"] in {"approval.request", "tool.result", "error"}
            for e in web.bus.drain_nowait()
        )
    finally:
        await _cleanup(web, task)


@pytest.mark.asyncio
async def test_stop_running_tool_finishes_cleanup_and_persists_all_call_results(
    tmp_path: Path,
) -> None:
    provider = _FakeProvider(
        [_batch(), LLMResponse(content="Next turn completed", model="fake-model")]
    )
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path), provider=provider)
    tool = _BlockingTool()
    web.tools = [tool]
    web.approval_strategy._rules_engine = ApprovalRulesEngine()
    web.approval_mode_manager.set(ApprovalMode.PERMISSIVE)
    await web.initialize()
    interrupt = EventInterrupt()
    task = asyncio.create_task(
        web.handle_user_message("Run checks", interrupt=interrupt)
    )
    try:
        await asyncio.wait_for(tool.started.wait(), timeout=2)
        engagement_id = web.engagement_store.engagement_id
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=2)
        assert tool.cancelled.is_set()
        assert tool.calls == ["first"]
        assert len(provider.requests) == 1
        _assert_balanced_results(web)
        await asyncio.wait_for(
            web.handle_user_message("Continue", interrupt=EventInterrupt()), timeout=2
        )
        assert web.engagement_store.engagement_id == engagement_id
        assert web.conversation_history[-1].content == "Next turn completed"
        assert len(provider.requests) == 2
    finally:
        await _cleanup(web, task)


@pytest.mark.asyncio
async def test_stop_master_keeps_unrelated_detached_agent_running(
    tmp_path: Path,
) -> None:
    provider = _BlockingProvider()
    web = WebKerkovSession(WebEventBus(), config=_config(tmp_path), provider=provider)
    await web.initialize()
    web._apply_system_prompt_if_needed()
    agent_started = asyncio.Event()
    finish_agent = asyncio.Event()

    async def run_agent(agent):
        agent_started.set()
        await finish_agent.wait()
        return _report(agent, findings="Independent work completed")

    agent = _spawn(web, run_agent)
    interrupt = EventInterrupt()
    task = asyncio.create_task(
        web.handle_user_message("Master work", interrupt=interrupt)
    )
    try:
        await asyncio.wait_for(agent_started.wait(), timeout=2)
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=2)
        assert agent.state == AgentState.RUNNING
        assert agent.cancel_requested is False
        assert not agent.task_handle.done()
        assert web.agent_supervisor.get(agent.id) is agent
        finish_agent.set()
        await asyncio.wait_for(agent.task_handle, timeout=2)
        assert agent.state == AgentState.DONE
        assert agent.report.findings == "Independent work completed"
        assert agent.report_delivered is False
        web._deliver_agent_reports()
        assert "Independent work completed" in web.conversation_history[-1].content
    finally:
        finish_agent.set()
        await _cleanup(web, task)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_one_shot_exec_terminates_its_local_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])
    provider = _FakeProvider(
        [
            LLMResponse(
                content="",
                model="fake-model",
                tool_calls=[
                    ToolCall(
                        tool_name="exec",
                        parameters={
                            "command": command,
                            "mode": "one_shot",
                            "stdio": "pipe",
                        },
                        call_id="local-process",
                    )
                ],
            )
        ]
    )
    web = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path, execution_enabled=True),
        provider=provider,
    )
    web.tools = [tool for tool in web.tools if isinstance(tool, ExecTool)]
    assert len(web.tools) == 1
    web.approval_strategy._rules_engine = ApprovalRulesEngine()
    web.approval_mode_manager.set(ApprovalMode.PERMISSIVE)
    await web.initialize()
    spawned = asyncio.Event()
    process_ids: list[str] = []
    original_spawn = web.process_supervisor.spawn

    async def track_spawn(**kwargs):
        managed = await original_spawn(**kwargs)
        process_ids.append(kwargs["run_id"])
        spawned.set()
        return managed

    monkeypatch.setattr(web.process_supervisor, "spawn", track_spawn)
    interrupt = EventInterrupt()
    task = asyncio.create_task(
        web.handle_user_message("Run command", interrupt=interrupt)
    )
    try:
        await asyncio.wait_for(spawned.wait(), timeout=3)
        process_id = process_ids[0]
        assert web.process_supervisor.get_active(process_id) is not None
        process_session = web.process_registry.get_session(process_id)
        assert process_session is not None
        _stop(web, interrupt)
        await asyncio.wait_for(task, timeout=3)
        # The process adapter settles exit asynchronously after termination.
        for _ in range(100):
            if web.process_supervisor.get_active(process_id) is None:
                break
            await asyncio.sleep(0.02)
        assert web.process_supervisor.get_active(process_id) is None
        assert process_session.exited
        assert web.process_registry.get_session(process_id) is None
        results = [m for m in web.conversation_history if m.role == "tool"]
        assert len(results) == 1
        assert results[0].call_id == "local-process"
        assert "interrupt" in results[0].content.lower()
        assert len(provider.requests) == 1
        assert not web._closed
    finally:
        await _cleanup(web, task)
