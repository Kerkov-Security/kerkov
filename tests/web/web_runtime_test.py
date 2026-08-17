"""Tests for the Django web runtime adapter."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field

from kerkov.core.config_manager import ConfigManager
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult
from kerkov.tools.browser import BrowserTool
from kerkov.tools.exec_tool import ExecMode, ExecStdio, ExecTool, ExecToolParams
from kerkov.tools.process_tool import ProcessTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession


class _FakeProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(api_key="test", model="fake-model")
        self.responses = responses
        self.requests: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[BaseTool]] = None,
    ) -> LLMResponse:
        self.requests.append(messages)
        return self.responses.pop(0)

    def validate_connection(self) -> bool:
        return True

    def list_models(self) -> list[str]:
        return [self.model]

    async def get_max_context_tokens(self) -> Optional[int]:
        return 100_000

    async def get_max_output_tokens(self) -> Optional[int]:
        return 2048


class _EchoParams(ToolParams):
    text: str = Field(default="")


class _EchoTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("echo", "echo text")

    def get_params_class(self):
        return _EchoParams

    def validate_params(self, toolParams: _EchoParams) -> Optional[str]:
        return None

    async def run(self, toolParams: _EchoParams) -> ToolResult:
        return ToolResult(success=True, output=f"echo:{toolParams.text}")


def _config(tmp_path: Path, *, execution_enabled: bool = False) -> ConfigManager:
    return ConfigManager(
        GOOGLE_API_KEY="test",
        EXECUTION_ENABLED=execution_enabled,
        WORKSPACE_DIR=str(tmp_path / "workspaces"),
        USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
        HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
    )


async def _collect_until(
    bus: WebEventBus,
    event_type: str,
    *,
    timeout: float = 1.0,
) -> list[dict]:
    events: list[dict] = []
    while True:
        event = await asyncio.wait_for(bus.next_event(), timeout=timeout)
        events.append(event)
        if event["type"] == event_type:
            return events


@pytest.mark.asyncio
async def test_web_session_runs_real_engine_turn(tmp_path: Path) -> None:
    bus = WebEventBus()
    provider = _FakeProvider(
        [
            LLMResponse(
                content="hello from the web runtime",
                model="fake-model",
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )
        ]
    )
    session = WebKerkovSession(bus, config=_config(tmp_path), provider=provider)

    await session.initialize()
    try:
        await session.handle_user_message("hello")
    finally:
        session.close()
    events = bus.drain_nowait()

    assert [event["type"] for event in events][0] == "session.ready"
    assert any(event["type"] == "workspace.ready" for event in events)
    assert any(
        event["type"] == "assistant.message"
        and event["payload"]["message"] == "hello from the web runtime"
        for event in events
    )
    assert [message.role for message in session.conversation_history] == [
        "system",
        "user",
        "assistant",
    ]
    assert provider.requests


@pytest.mark.asyncio
async def test_web_session_waits_for_browser_tool_approval(tmp_path: Path) -> None:
    bus = WebEventBus()
    provider = _FakeProvider(
        [
            LLMResponse(
                content="",
                model="fake-model",
                tool_calls=[
                    ToolCall(
                        tool_name="echo",
                        parameters={"text": "approved"},
                        call_id="call-1",
                    )
                ],
            ),
            LLMResponse(content="done", model="fake-model"),
        ]
    )
    session = WebKerkovSession(bus, config=_config(tmp_path), provider=provider)
    session.tools = [_EchoTool()]

    await session.initialize()
    task = asyncio.create_task(session.handle_user_message("use a tool"))
    events = await _collect_until(bus, "approval.request")
    approval = events[-1]

    assert approval["payload"]["tool"] == "echo"

    assert session.resolve_approval(
        approval["payload"]["approval_id"],
        "approve",
    )
    await asyncio.wait_for(task, timeout=1)
    session.close()
    events.extend(bus.drain_nowait())

    assert any(
        event["type"] == "tool.result" and event["payload"]["success"]
        for event in events
    )
    assert any(
        event["type"] == "assistant.message" and event["payload"]["message"] == "done"
        for event in events
    )


@pytest.mark.asyncio
async def test_disconnect_denies_pending_approval_without_task_cancellation(
    tmp_path: Path,
) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    task = asyncio.create_task(
        session.approval_strategy.approve(_EchoTool(), {"text": "pending"})
    )
    await _collect_until(bus, "approval.request")

    session.approvals.cancel_all()
    result = await task

    assert result.approved is False
    assert result.user_message == "Browser session disconnected before approval."


@pytest.mark.asyncio
async def test_approval_strategy_propagates_task_cancellation(tmp_path: Path) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    task = asyncio.create_task(
        session.approval_strategy.approve(_EchoTool(), {"text": "pending"})
    )
    await _collect_until(bus, "approval.request")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_approve_save_survives_workspace_persistence_failure(
    tmp_path: Path,
) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    session.approval_strategy.set_workspace(tmp_path)
    tool = _EchoTool()

    with patch(
        "kerkov.web.runtime.approval.append_rule_to_file",
        side_effect=OSError("disk full sentinel"),
    ):
        task = asyncio.create_task(
            session.approval_strategy.approve(tool, {"text": "approved"})
        )
        approval = (await _collect_until(bus, "approval.request"))[-1]
        assert session.resolve_approval(
            approval["payload"]["approval_id"],
            "approve_save",
        )
        result = await asyncio.wait_for(task, timeout=1)

    events = bus.drain_nowait()
    warning = next(event for event in events if event["type"] == "log.warning")

    assert result.approved is True
    assert "could not be saved" in warning["payload"]["message"]
    assert "disk full sentinel" not in warning["payload"]["message"]

    repeat = await session.approval_strategy.approve(tool, {"text": "approved"})
    assert repeat.approved is True
    assert any(event["type"] == "approval.auto" for event in bus.drain_nowait())


def test_web_sessions_isolate_process_tools(tmp_path: Path) -> None:
    first = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    second = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )

    first_exec = next(tool for tool in first.tools if isinstance(tool, ExecTool))
    first_process = next(tool for tool in first.tools if isinstance(tool, ProcessTool))
    second_exec = next(tool for tool in second.tools if isinstance(tool, ExecTool))

    assert first_exec.registry is first_process.registry
    assert first_exec.supervisor is first_process.supervisor
    assert first_exec.registry is not second_exec.registry
    assert first_exec.supervisor is not second_exec.supervisor
    assert not any(isinstance(tool, BrowserTool) for tool in first.tools)


@pytest.mark.asyncio
async def test_selecting_current_mode_preserves_session_state(tmp_path: Path) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    message = Message(role="user", content="keep this conversation")
    session.conversation_history = [message]
    session.token_tracker.record_usage(12, 3)
    current_mode = session.mode_manager.current_mode.mode_type.value

    await session.handle_user_message(f"/mode {current_mode}")
    events = bus.drain_nowait()

    assert session.conversation_history == [message]
    assert session.token_tracker.total_tokens == 15
    assert any(event["type"] == "mode.info" for event in events)
    assert not any(event["type"] == "mode.changed" for event in events)


@pytest.mark.asyncio
async def test_web_session_close_closes_the_knowledge_base(tmp_path: Path) -> None:
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider([LLMResponse(content="hi", model="fake-model")]),
    )

    await session.initialize()
    await session.handle_user_message("hello")
    opened = session.knowledge_base
    session.close()

    assert opened is not None
    # The SQLite handle is released, not just dropped on the floor.
    assert opened._conn is None
    assert session.knowledge_base is None


@pytest.mark.asyncio
async def test_web_session_wires_the_findings_ledger(tmp_path: Path) -> None:
    """The mode prompt tells the model to use `ledger`, so the console must have it."""
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider([LLMResponse(content="hi", model="fake-model")]),
    )

    await session.initialize()
    await session.handle_user_message("hello")
    ledger_tool = next(t for t in session.tools if t.name == "ledger")
    result = await ledger_tool.execute(
        {"op": "add", "section": "flags", "entry": {"value": "HTB{web}"}}
    )
    session.close()

    assert result.success, result.error
    assert session.compactor is not None


@pytest.mark.asyncio
async def test_clear_closes_the_previous_engagements_knowledge_base(
    tmp_path: Path,
) -> None:
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider(
            [
                LLMResponse(content="first", model="fake-model"),
                LLMResponse(content="second", model="fake-model"),
            ]
        ),
    )

    await session.initialize()
    await session.handle_user_message("hello")
    first = session.knowledge_base
    await session.handle_user_message("/clear")
    await session.handle_user_message("hello again")
    session.close()

    assert first is not None
    assert session.knowledge_base is not first
    assert first._conn is None


def test_web_session_close_cancels_owned_processes(tmp_path: Path) -> None:
    registry = SessionRegistry()
    supervisor = MagicMock(spec=ProcessSupervisor)
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path),
        provider=_FakeProvider([]),
        process_registry=registry,
        process_supervisor=supervisor,
    )

    session.close()

    supervisor.cancel_all.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_web_session_disconnect_kills_owned_background_process(
    tmp_path: Path,
) -> None:
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path, execution_enabled=True),
        provider=_FakeProvider([]),
    )
    exec_tool = next(tool for tool in session.tools if isinstance(tool, ExecTool))
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    result = await exec_tool.run(
        ExecToolParams(
            command=command,
            mode=ExecMode.DAEMON,
            stdio=ExecStdio.PIPE,
        )
    )
    process_id = result.metadata["session_id"]
    assert session.process_supervisor.get_active(process_id) is not None

    session.close()
    for _ in range(100):
        if session.process_supervisor.get_active(process_id) is None:
            break
        await asyncio.sleep(0.02)

    assert session.process_supervisor.get_active(process_id) is None
    finished = session.process_registry.get_finished_session(process_id)
    assert finished is not None
    assert finished.status == "killed"


@pytest.mark.asyncio
async def test_web_session_hides_internal_exception_details(tmp_path: Path) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path),
        provider=_FakeProvider([]),
    )
    session._run_engine_turn = AsyncMock(
        side_effect=RuntimeError("sentinel-secret /private/operator/path")
    )

    await session.handle_user_message("trigger failure")
    error = next(event for event in bus.drain_nowait() if event["type"] == "error")

    assert error["payload"] == {
        "detail": "The request failed. Check the server logs for details."
    }
