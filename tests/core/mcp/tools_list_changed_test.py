"""Opt-in `toolsListChanged`: a flagged change, applied only when asked."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp import connection
from kerkov.core.mcp.manager import McpManager

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.client.subscriptions import SubscriptionLost  # noqa: E402
from mcp.server.caching import CacheHint  # noqa: E402
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


class _Subscription:
    """An acknowledged tool watch that never delivers an event."""

    honored = SimpleNamespace(tools_list_changed=True)

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> Any:
        await asyncio.Event().wait()


class _DroppingClient:
    """Loses its first watch, then resubscribes once `resubscribe` is set."""

    def __init__(self) -> None:
        self.resubscribe = asyncio.Event()
        self.listens = 0

    async def __aenter__(self) -> "_DroppingClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def list_tools(self, cursor=None, cache_mode="use") -> Any:
        tool = SimpleNamespace(name="scan", description="", input_schema={})
        return SimpleNamespace(tools=[tool], next_cursor=None)

    @asynccontextmanager
    async def listen(self, tools_list_changed: bool):
        self.listens += 1
        if self.listens == 1:
            raise SubscriptionLost("stream dropped")
        await self.resubscribe.wait()
        yield _Subscription()


def _manager(
    tmp_path: Path, entry: Dict[str, Any], factory: Optional[Any] = None
) -> McpManager:
    if factory is None:
        server = _server()
        factory = lambda: Client(server)  # noqa: E731
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"lab": entry}}), encoding="utf-8")
    return McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: factory,
        environment={},
    )


async def _settle(manager: McpManager, key: str = "lab") -> bool:
    """Let the watch deliver the event; it arrives after the call returns."""
    for _ in range(100):
        if manager._servers[key].stale:
            return True
        await asyncio.sleep(0.01)
    return False


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


def test_a_refetch_reads_past_the_clients_cached_listing(tmp_path: Path) -> None:
    server = MCPServer("lab", cache_hints={"tools/list": CacheHint(ttl_ms=600_000)})

    @server.tool()
    def scan(target: str) -> str:
        """Scan a target."""
        return target

    def extra(target: str) -> str:
        """A tool added while nobody was watching."""
        return target

    manager = _manager(
        tmp_path, {"command": "x", "toolsListChanged": True}, lambda: Client(server)
    )

    async def scenario():
        await manager.start(set())
        server.add_tool(extra)
        manager._mark_stale("lab")
        changed = await manager.apply_tool_changes()
        names = sorted(t.original_name for t in manager.tools())
        await manager.close()
        return changed, names

    changed, names = asyncio.run(scenario())
    assert changed == ["lab"]
    assert names == ["extra", "scan"]


def test_a_reestablished_watch_marks_the_unwatched_gap_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "WATCH_RETRY_SECONDS", 0.01)
    client = _DroppingClient()
    manager = _manager(
        tmp_path, {"command": "x", "toolsListChanged": True}, lambda: client
    )

    async def scenario():
        await manager.start(set())
        dropped = await _settle(manager)
        await manager.apply_tool_changes()
        gap = manager.status()[0].watch, manager._servers["lab"].stale
        client.resubscribe.set()
        resubscribed = await _settle(manager)
        watch = manager.status()[0].watch
        await manager.close()
        return dropped, gap, resubscribed, watch

    dropped, gap, resubscribed, watch = asyncio.run(scenario())
    assert dropped is True
    assert gap == ("reconnecting", False)
    assert resubscribed is True
    assert watch == "watching"
