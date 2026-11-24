"""MCP servers in the web runtime: registration, engagement switches, cleanup."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional
from unittest.mock import MagicMock

import pytest

from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.core.cancellable_task import EventInterrupt
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp import connection as mcp_connection
from kerkov.core.mcp.manager import McpManager
from kerkov.models.basic_models import LLMResponse
from kerkov.tools.mcp.mcp_tool import McpTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.mcp import handle_mcp_message, mcp_status
from kerkov.web.runtime.session import WebKerkovSession, web_command
from tests.web.web_runtime_test import _FakeProvider

try:
    # Module level: this file postpones annotations, and the SDK resolves a
    # tool's `ctx: Context` against the module's globals.
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover - the MCP extra is absent
    Context = None


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

        for command in ("/mcp", "/mcp list"):
            await session.handle_user_message(command)
            [result] = _results(session)
            assert result["failed"] is True
            assert result["status"]["load_error"] in result["detail"]
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


_PID_SERVER = """
import os, sys
from mcp.server.mcpserver import MCPServer

with open(sys.argv[1], "w") as handle:
    handle.write(str(os.getpid()))
server = MCPServer("fast")


@server.tool()
def ping() -> str:
    return "pong"


server.run()
"""


def _alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@asynccontextmanager
async def _stalled_client() -> AsyncIterator[Any]:
    await asyncio.Event().wait()
    yield None


@pytest.mark.asyncio
async def test_cancelled_startup_stops_servers_that_already_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    monkeypatch.setattr(mcp_connection, "CLOSE_TIMEOUT_SECONDS", 0.5)
    script = tmp_path / "pid_server.py"
    script.write_text(_PID_SERVER)
    pid_file = tmp_path / "server.pid"
    session = _session(tmp_path)
    path = tmp_path / "mcp.json"
    fast = {"command": sys.executable, "args": [str(script), str(pid_file)]}
    path.write_text(
        json.dumps({"mcpServers": {"fast": fast, "slow": {"command": "unused"}}})
    )
    session.mcp = McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: (
            _stalled_client if resolved.config.key == "slow" else None
        ),
        environment={},
        on_warning=session._warn_mcp,
    )
    manager = session.mcp

    # A disconnect before `session.ready` reaches the consumer as cancellation.
    startup = asyncio.create_task(session.initialize())
    for _ in range(300):
        states = {row.key: row.state for row in manager.status()}
        if states.get("fast") == "connected":
            break
        await asyncio.sleep(0.1)
    assert states.get("fast") == "connected"
    pid = int(pid_file.read_text())
    assert _alive(pid)

    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    for _ in range(100):
        if not _alive(pid):
            break
        await asyncio.sleep(0.1)
    assert not _alive(pid)
    assert [row.state for row in manager.status()] == ["disconnected"] * 2


@pytest.mark.asyncio
async def test_status_request_reports_servers_and_their_tools(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_sdk_server())
    await session.initialize()
    session.bus.drain_nowait()
    try:
        handle_mcp_message(session, {"type": "mcp.status", "request_id": "r1"})
        [event] = session.bus.drain_nowait()

        assert event["type"] == "mcp.status"
        payload = event["payload"]
        assert payload["request_id"] == "r1"
        assert payload["enabled"] is True
        assert payload["config_path"] == str(tmp_path / "mcp.json")
        assert payload["load_error"] is None
        assert payload["servers"] == [
            {
                "key": "demo",
                "transport": "stdio",
                "enabled": True,
                "state": "connected",
                "auth": "none",
                "trusted": False,
                "resources": None,
                "watch": None,
                "advertised": 1,
                "exposed": 1,
                "tools": [{"name": "mcp__demo__add", "original": "add"}],
                "skipped": [],
            }
        ]
    finally:
        await session.aclose()


@pytest.mark.parametrize(
    "content",
    [
        {"type": "mcp.status"},
        {"type": "mcp.status", "request_id": "bad id!"},
        {"type": "mcp.status", "request_id": "r1", "server": "demo"},
    ],
)
def test_malformed_status_requests_are_refused(content: dict) -> None:
    session = SimpleNamespace(bus=WebEventBus(), mcp=None)

    handle_mcp_message(session, content)
    [event] = session.bus.drain_nowait()

    assert event["type"] == "mcp.error"


def test_status_says_when_mcp_is_disabled() -> None:
    assert mcp_status(None) == {"enabled": False, "servers": [], "warnings": []}


def _results(session: WebKerkovSession) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in session.bus.drain_nowait()
        if event["type"] == "mcp.result"
    ]


@pytest.mark.asyncio
async def test_mcp_commands_reregister_tools_without_a_model_turn(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, server=_sdk_server())
    await session.initialize()
    session.bus.drain_nowait()
    provider = session.provider
    assert isinstance(provider, _FakeProvider)
    try:
        await session.handle_user_message("/mcp disconnect demo")
        [result] = _results(session)
        assert result["title"] == "Disconnected demo"
        assert "prompt cache" in result["detail"]
        assert result["status"]["servers"][0]["state"] == "disconnected"
        assert "mcp__demo__add" not in [t.name for t in session.tools]
        assert "mcp__demo__add" not in [
            t.name for t in _spawn_tool(session)._master_tools
        ]

        await session.handle_user_message("/mcp connect")
        [result] = _results(session)
        assert result["failed"] is False
        assert result["view"] == "message"
        assert result["detail"].splitlines()[0] == "Connected demo"
        names = [tool.name for tool in session.tools]
        assert names[-3:] == ["mcp__demo__add", "delegate", "agent_wait"]

        await session.handle_user_message("/mcp list")
        [result] = _results(session)
        assert result["detail"] == "demo (connected)\n  mcp__demo__add \u2190 add"
        assert (result["view"], result["server"]) == ("list", None)

        await session.handle_user_message("/mcp list demo")
        [result] = _results(session)
        assert (result["view"], result["server"]) == ("list", "demo")

        await session.handle_user_message("/mcp")
        [result] = _results(session)
        assert result["detail"].splitlines()[1].startswith("demo \u00b7 stdio")
        assert result["view"] == "status"

        assert provider.requests == []
        assert session.conversation_history == []
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_connect_reports_servers_removed_from_mcp_json(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_sdk_server())
    await session.initialize()
    session.bus.drain_nowait()
    try:
        (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {}}))

        await session.handle_user_message("/mcp connect")

        [result] = _results(session)
        assert result["detail"].splitlines() == [
            "Disconnected demo (no longer in mcp.json)",
            "Every enabled MCP server is already connected.",
            "The advertised tool list changed; the next request rebuilds the "
            "provider's prompt cache.",
        ]
        assert not any(isinstance(t, McpTool) for t in session.tools)
        assert result["status"]["servers"] == []

        await session.handle_user_message("/mcp list")
        [result] = _results(session)
        assert (result["title"], result["failed"]) == ("MCP tools", False)
        assert result["detail"] == "No MCP servers configured."
    finally:
        await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected",
    [
        ("/mcp key demo", "never in the browser"),
        ("/mcp login demo", "never in the browser"),
        ("/mcp logout demo", "never in the browser"),
        ("/mcp add demo --url https://example.test/mcp", "does not edit mcp.json"),
        ("/mcp remove demo", "does not edit mcp.json"),
        ("/mcp disconnect", "Use `/mcp`"),
        ("/mcp connect a b", "Use `/mcp`"),
    ],
)
async def test_cli_only_and_malformed_commands_change_nothing(
    tmp_path: Path, command: str, expected: str
) -> None:
    session = _session(tmp_path)
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message(command)

        [result] = _results(session)
        assert result["failed"] is True
        assert expected in result["detail"]
        assert (tmp_path / "mcp.json").read_text() == json.dumps(
            {"mcpServers": {"demo": {"command": "unused"}}}
        )
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_mcp_command_explains_when_mcp_is_disabled(tmp_path: Path) -> None:
    session = _session(tmp_path, MCP_ENABLED=False)
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message("/mcp connect")

        [result] = _results(session)
        assert result["title"] == "MCP is disabled"
        assert result["status"] == {"enabled": False, "servers": [], "warnings": []}
    finally:
        await session.aclose()


def test_mcp_commands_are_local_and_not_stoppable() -> None:
    assert web_command("/mcp") == "mcp"
    assert web_command("/mcp connect burp") == "mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_json,command,title,detail,failed",
    [
        (
            '{"mcpServers": {}}',
            "/mcp list",
            "MCP tools",
            "No MCP servers configured.",
            False,
        ),
        ('{"mcpServers": {}}', "/mcp list nope", "No MCP server named nope", "", True),
        ("{not json", "/mcp list", "MCP tools", None, True),
    ],
)
async def test_list_without_a_matching_server_says_why(
    tmp_path: Path,
    mcp_json: str,
    command: str,
    title: str,
    detail: Optional[str],
    failed: bool,
) -> None:
    session = _session(tmp_path)
    (tmp_path / "mcp.json").write_text(mcp_json)
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message(command)

        [result] = _results(session)
        assert session.mcp is not None
        assert result["title"] == title
        assert result["detail"] == (
            detail if detail is not None else session.mcp.load_error
        )
        assert result["failed"] is failed
        assert "None" not in result["title"] + result["detail"]
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_list_marks_a_trusted_server(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_sdk_server())
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "unused", "trusted": True}}})
    )
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message("/mcp list")

        [result] = _results(session)
        assert result["detail"].splitlines()[0] == "demo (connected, trusted)"
    finally:
        await session.aclose()


def _prompt_server() -> Any:
    server = _sdk_server()

    @server.prompt(description="Triage one CVE.")
    def triage(cve: str) -> str:
        return f"Triage {cve}."

    return server


@pytest.mark.asyncio
async def test_a_prompt_becomes_the_user_turn(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_prompt_server())
    await session.initialize()
    session.bus.drain_nowait()
    provider = session.provider
    assert isinstance(provider, _FakeProvider)
    try:
        await session.handle_user_message("/mcp prompts")
        [listing] = _results(session)
        assert "triage(cve*): Triage one CVE." in listing["detail"]
        assert provider.requests == []

        await session.handle_user_message("/mcp prompt demo triage cve=CVE-2026-0001")

        assert session.conversation_history[-2].content == "Triage CVE-2026-0001."
        assert session.conversation_history[-2].role == "user"
        assert len(provider.requests) == 1
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_a_prompt_missing_an_argument_starts_no_turn(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_prompt_server())
    await session.initialize()
    session.bus.drain_nowait()
    provider = session.provider
    assert isinstance(provider, _FakeProvider)
    try:
        await session.handle_user_message("/mcp prompt demo triage")

        [result] = _results(session)
        assert result["failed"] is True
        assert result["detail"] == "triage needs cve=<value>"
        assert provider.requests == []
        assert session.conversation_history == []
    finally:
        await session.aclose()


def test_a_prompt_is_a_stoppable_turn_not_a_local_command() -> None:
    assert web_command("/mcp prompt demo triage cve=x") is None
    assert web_command("/mcp prompt demo triage") is None
    assert web_command("/mcp prompt demo") == "mcp"
    assert web_command("/mcp prompts") == "mcp"


@pytest.mark.asyncio
async def test_an_incomplete_prompt_command_gets_a_usage_reply(tmp_path: Path) -> None:
    session = _session(tmp_path, server=_prompt_server())
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message("/mcp prompt demo")

        [result] = _results(session)
        assert result["failed"] is True
        assert "/mcp prompt <server> <prompt>" in result["detail"]
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_a_first_turn_prompt_saves_attachments_to_its_workspace(
    tmp_path: Path,
) -> None:
    from mcp.server.mcpserver.prompts.base import UserMessage
    from mcp.types import ImageContent

    server = _prompt_server()
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

    @server.prompt(description="Review a screenshot.")
    def review() -> list:
        return [
            UserMessage("Review this login page."),
            UserMessage(ImageContent(type="image", data=image, mime_type="image/png")),
        ]

    session = _session(tmp_path, server=server)
    await session.initialize()
    session.bus.drain_nowait()
    try:
        await session.handle_user_message("/mcp prompt demo review")

        assert session.workspace_path is not None
        [saved] = (session.workspace_path / "mcp" / "demo").iterdir()
        assert saved.read_bytes() == base64.b64decode(image)
        turn = session.conversation_history[-2]
        assert turn.role == "user"
        assert f"saved to {saved}" in turn.content
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_stop_cancels_a_prompt_that_is_still_loading(tmp_path: Path) -> None:
    server = _prompt_server()
    loading = asyncio.Event()

    @server.prompt(description="Never finishes.")
    async def stall() -> str:
        loading.set()
        await asyncio.Event().wait()
        return "unreachable"

    session = _session(tmp_path, server=server)
    await session.initialize()
    session.bus.drain_nowait()
    provider = session.provider
    assert isinstance(provider, _FakeProvider)
    interrupt = EventInterrupt()
    try:
        turn = asyncio.create_task(
            session.handle_user_message("/mcp prompt demo stall", interrupt=interrupt)
        )
        await asyncio.wait_for(loading.wait(), timeout=10)

        interrupt.interrupt_requested.set()
        await asyncio.wait_for(turn, timeout=10)

        messages = [
            event["payload"].get("message")
            for event in session.bus.drain_nowait()
            if event["type"] == "log.info"
        ]
        assert "Run stopped by user" in messages
        assert provider.requests == []
        assert session.conversation_history == []
        assert not session._lock.locked()
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_announced_tool_changes_register_at_the_next_message(
    tmp_path: Path,
) -> None:
    server = _sdk_server()

    def extra(target: str) -> str:
        """A tool that appears later."""
        return target

    @server.tool()
    async def unlock(ctx: Context) -> str:
        """Add the extra tool and announce it."""
        server.add_tool(extra)
        await ctx.notify_tools_changed()
        return "unlocked"

    session = _session(tmp_path, server=server)
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"demo": {"command": "unused", "toolsListChanged": True}}}
        )
    )
    await session.initialize()
    session.bus.drain_nowait()
    try:
        unlock_tool = next(t for t in session.tools if t.name == "mcp__demo__unlock")
        await unlock_tool.execute({})
        for _ in range(100):
            if session.mcp._servers["demo"].stale:
                break
            await asyncio.sleep(0.01)
        assert "mcp__demo__extra" not in [t.name for t in session.tools]

        await session.handle_user_message("/mcp list")

        changed, listed = _results(session)
        assert changed["title"] == "MCP tools changed"
        assert "prompt cache" in changed["detail"]
        assert "tool changes: watching" in listed["detail"].splitlines()[0]
        assert "mcp__demo__extra" in [t.name for t in session.tools]
        assert "mcp__demo__extra" in [
            t.name for t in _spawn_tool(session)._master_tools
        ]
    finally:
        await session.aclose()
