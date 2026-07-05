"""Tests for the Django web runtime adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest
from pydantic import Field

from kerkov.core.config_manager import ConfigManager
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult
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


def _config(tmp_path: Path) -> ConfigManager:
    return ConfigManager(
        GOOGLE_API_KEY="test",
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
    await session.handle_user_message("hello")
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
    await task
    events.extend(bus.drain_nowait())

    assert any(
        event["type"] == "tool.result" and event["payload"]["success"]
        for event in events
    )
    assert any(
        event["type"] == "assistant.message" and event["payload"]["message"] == "done"
        for event in events
    )
