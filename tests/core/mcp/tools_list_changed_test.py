"""Opt-in `toolsListChanged`: a flagged change, applied only when asked."""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.server.mcpserver import Context, MCPServer  # noqa: E402


def _server() -> MCPServer:
    server = MCPServer("lab")

    def extra(target: str) -> str:
        """A tool that appears later."""
        return f"extra on {target}"

    @server.tool()
    async def unlock(ctx: Context) -> str:
        """Add the extra tool and announce it."""
        server.add_tool(extra)
        await ctx.notify_tools_changed()
        return "unlocked"

    @server.tool()
    async def touch(ctx: Context) -> str:
        """Announce a change without changing anything."""
        await ctx.notify_tools_changed()
        return "touched"

    return server


def _manager(tmp_path: Path, entry: Dict[str, Any]) -> McpManager:
    server = _server()
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"lab": entry}}), encoding="utf-8")
    return McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: lambda: Client(server),
        environment={},
    )


async def _settle(manager: McpManager, key: str = "lab") -> None:
    """Let the watch deliver the event; it arrives after the call returns."""
    for _ in range(100):
        if manager._servers[key].stale:
            return
        await asyncio.sleep(0.01)


def test_a_watched_change_is_applied_only_at_the_next_boundary(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"command": "x", "toolsListChanged": True})

    async def scenario():
        await manager.start(set())
        watch = manager.status()[0].watch
        tools = {tool.original_name: tool for tool in manager.tools()}
        await tools["unlock"].execute({})
        await _settle(manager)
        before = sorted(t.original_name for t in manager.tools())
        changed = await manager.apply_tool_changes()
        after = sorted(t.original_name for t in manager.tools())
        again = await manager.apply_tool_changes()
        await manager.close()
        return watch, before, changed, after, again

    watch, before, changed, after, again = asyncio.run(scenario())
    assert watch == "watching"
    assert before == ["touch", "unlock"]
    assert changed == ["lab"]
    assert after == ["extra", "touch", "unlock"]
    assert again == []


def test_an_announcement_without_a_change_re_registers_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"command": "x", "toolsListChanged": True})

    async def scenario():
        await manager.start(set())
        tools = {tool.original_name: tool for tool in manager.tools()}
        await tools["touch"].execute({})
        await _settle(manager)
        flagged = manager._servers["lab"].stale
        changed = await manager.apply_tool_changes()
        await manager.close()
        return flagged, changed

    flagged, changed = asyncio.run(scenario())
    assert flagged is True
    assert changed == []


def test_without_the_opt_in_nothing_is_watched(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"command": "x"})

    async def scenario():
        await manager.start(set())
        watch = manager.status()[0].watch
        tools = {tool.original_name: tool for tool in manager.tools()}
        await tools["unlock"].execute({})
        await asyncio.sleep(0.1)
        changed = await manager.apply_tool_changes()
        names = sorted(t.original_name for t in manager.tools())
        await manager.close()
        return watch, changed, names

    watch, changed, names = asyncio.run(scenario())
    assert watch is None
    assert changed == []
    assert names == ["touch", "unlock"]


def test_closing_a_watched_connection_finishes_promptly(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"command": "x", "toolsListChanged": True})

    async def scenario():
        await manager.start(set())
        loop = asyncio.get_running_loop()
        started = loop.time()
        await manager.close()
        return loop.time() - started, manager.status()[0].state

    elapsed, state = asyncio.run(scenario())
    assert elapsed < 5
    assert state == "disconnected"
