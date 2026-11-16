"""MCP servers in the web runtime: registration, engagement switches, cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager
from kerkov.models.basic_models import LLMResponse
from kerkov.tools.mcp.mcp_tool import McpTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.web_runtime_test import _FakeProvider


def _config(tmp_path: Path, **overrides: Any) -> ConfigManager:
    return ConfigManager(
        GOOGLE_API_KEY="test",
        WORKSPACE_DIR=str(tmp_path / "workspaces"),
        USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
        HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
        **overrides,
    )


def _sdk_server() -> Any:
    pytest.importorskip("mcp")
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    return server


def _session(
    tmp_path: Path, *, server: Optional[Any] = None, **config: Any
) -> WebKerkovSession:
    """A session whose `mcp.json` names one in-process SDK server, `demo`."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "unused"}}}))
    session = WebKerkovSession(
        WebEventBus(),
        config=_config(tmp_path, **config),
        provider=_FakeProvider(
            [LLMResponse(content="ok", model="fake-model") for _ in range(4)]
        ),
    )
    if server is not None:
        from mcp import Client

        assert session.mcp is not None
        session.mcp = McpManager(
            path,
            credentials=CredentialManager(
                environment={}, persistent_store=MemorySecretStore()
            ),
            client_factory_provider=lambda _resolved: lambda: Client(server),
            environment={},
            on_warning=session._warn_mcp,
        )
    return session


def _spawn_tool(session: WebKerkovSession) -> SpawnAgentTool:
    return next(tool for tool in session.tools if isinstance(tool, SpawnAgentTool))


@pytest.mark.asyncio
async def test_mcp_tools_are_registered_before_session_ready(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_sdk_server())

    await session.initialize()
    try:
        events = session.bus.drain_nowait()
        ready = next(e for e in events if e["type"] == "session.ready")
        assert "mcp__demo__add" in ready["payload"]["tools"]
        assert any(
            e["type"] == "log.info"
            and "1 server(s) connected" in e["payload"]["message"]
            for e in events
        )
        # Future agents and nested delegates draw from the same pool, which a
        # profile must still name a tool from before an agent receives it.
        assert "mcp__demo__add" in [t.name for t in _spawn_tool(session)._master_tools]
        assert isinstance(session.tools[-1], AgentWaitTool)

        result = await next(
            t for t in session.tools if t.name == "mcp__demo__add"
        ).execute({"a": 2, "b": 3})
        assert result.success and result.output == "5"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_engagement_switches_keep_connections_and_cancel_calls(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, server=_sdk_server())
    await session.initialize()
    assert session.mcp is not None
    cancel = MagicMock(wraps=session.mcp.cancel_inflight)
    session.mcp.cancel_inflight = cancel  # type: ignore[method-assign]
    try:
        await session.handle_user_message("hello")
        await session.handle_user_message("/clear")
        await session.handle_user_message("/mode red")

        assert cancel.call_count == 2
        assert [row.state for row in session.mcp.status()] == ["connected"]
        names = [tool.name for tool in session.tools]
        assert names.count("mcp__demo__add") == 1
        # The rebuilt delegate keeps the order the first engagement advertised.
        assert names[-2:] == ["delegate", "agent_wait"]
        assert "mcp__demo__add" in [t.name for t in _spawn_tool(session)._master_tools]
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_disconnect_closes_every_connection(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_sdk_server())
    await session.initialize()
    assert session.mcp is not None

    await session.aclose()

    assert [row.state for row in session.mcp.status()] == ["disconnected"]
    assert session.mcp.tools() == []


@pytest.mark.asyncio
async def test_a_broken_mcp_json_warns_but_the_console_still_starts(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    (tmp_path / "mcp.json").write_text("{not json")

    await session.initialize()
    try:
        events = session.bus.drain_nowait()
        assert any(
            e["type"] == "log.warning"
            and e["payload"]["message"].startswith("MCP disabled")
            for e in events
        )
        ready = next(e for e in events if e["type"] == "session.ready")
        assert ready["payload"]["provider_ready"] is True
        assert not any(isinstance(t, McpTool) for t in session.tools)
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_mcp_disabled_builds_no_manager(tmp_path: Path) -> None:
    session = _session(tmp_path, MCP_ENABLED=False)

    await session.initialize()
    try:
        assert session.mcp is None
        assert any(e["type"] == "session.ready" for e in session.bus.drain_nowait())
    finally:
        await session.aclose()
