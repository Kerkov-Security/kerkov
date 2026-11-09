"""McpManager: discovery, frozen tool sets, calls, failures and cleanup."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager

_SCHEMA = {
    "type": "object",
    "properties": {"host": {"type": "string"}},
    "required": ["host"],
}


class _FakeClient:
    """Stands in for the SDK client: `async with`, list_tools, call_tool."""

    def __init__(self, tools: List[Dict[str, Any]], *, fail: Optional[str] = None):
        self.tools = tools
        self.fail = fail
        self.entered = False
        self.exited = False
        self.calls: List[tuple] = []
        self.gate: Optional[asyncio.Event] = None

    async def __aenter__(self) -> "_FakeClient":
        if self.fail:
            raise RuntimeError(self.fail)
        self.entered = True
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.exited = True

    async def list_tools(self, cursor: Optional[str] = None) -> Any:
        page = 0 if cursor is None else int(cursor)
        chunk = self.tools[page : page + 2]
        more = page + 2 < len(self.tools)
        return SimpleNamespace(
            tools=[SimpleNamespace(**tool) for tool in chunk],
            next_cursor=str(page + 2) if more else None,
        )

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.gate is not None:
            await self.gate.wait()
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text=f"{name} on {arguments['host']}")
            ],
            structured_content=None,
            is_error=False,
        )


def _tool(name: str, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} tool",
        "input_schema": schema or _SCHEMA,
    }


def _credentials() -> CredentialManager:
    return CredentialManager(environment={}, persistent_store=MemorySecretStore())


def _manager(tmp_path: Path, servers: Dict[str, Any], clients: Dict[str, _FakeClient]):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return McpManager(
        path,
        credentials=_credentials(),
        client_factory_provider=lambda resolved: lambda: clients[resolved.config.key],
        environment={},
    )


def test_discovers_every_page_and_exposes_sorted_mapped_tools(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan"), _tool("report"), _tool("enum")])
    manager = _manager(tmp_path, {"burp": {"command": "x"}}, {"burp": client})

    async def scenario():
        await manager.start({"exec"})
        names = [tool.name for tool in manager.tools()]
        await manager.close()
        return names

    assert asyncio.run(scenario()) == [
        "mcp__burp__enum",
        "mcp__burp__report",
        "mcp__burp__scan",
    ]
    assert client.exited


def test_tool_filter_limits_what_is_advertised(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan"), _tool("delete")])
    manager = _manager(
        tmp_path,
        {"burp": {"command": "x", "tools": {"deny": ["delete"]}}},
        {"burp": client},
    )

    async def scenario():
        await manager.start(set())
        (row,) = manager.status()
        await manager.close()
        return row

    row = asyncio.run(scenario())
    assert (row.advertised, row.exposed) == (2, 1)


def test_unrepresentable_schema_skips_only_that_tool(tmp_path: Path) -> None:
    recursive = {
        "type": "object",
        "$defs": {"N": {"properties": {"next": {"$ref": "#/$defs/N"}}}},
        "properties": {"root": {"$ref": "#/$defs/N"}},
    }
    client = _FakeClient([_tool("ok"), _tool("tree", recursive)])
    manager = _manager(tmp_path, {"srv": {"command": "x"}}, {"srv": client})

    async def scenario():
        await manager.start(set())
        names = [tool.name for tool in manager.tools()]
        (row,) = manager.status()
        await manager.close()
        return names, row

    names, row = asyncio.run(scenario())
    assert names == ["mcp__srv__ok"]
    assert row.skipped[0][0] == "tree"


def test_calls_route_through_the_tool_with_validation(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan")])
    manager = _manager(tmp_path, {"burp": {"command": "x"}}, {"burp": client})

    async def scenario():
        await manager.start(set())
        (tool,) = manager.tools()
        good = await tool.execute({"host": "10.0.0.5"}, call_id="c1")
        bad = await tool.execute({"port": 1})
        await manager.close()
        return good, bad

    good, bad = asyncio.run(scenario())
    assert good.success and good.output == "scan on 10.0.0.5"
    assert good.metadata == {"mcp_server": "burp", "mcp_tool": "scan"}
    assert not bad.success and "host" in bad.error
    assert client.calls == [("scan", {"host": "10.0.0.5"})]


def test_failed_server_is_reported_and_others_still_connect(tmp_path: Path) -> None:
    clients = {
        "good": _FakeClient([_tool("scan")]),
        "bad": _FakeClient([], fail="spawn failed"),
    }
    manager = _manager(
        tmp_path, {"good": {"command": "x"}, "bad": {"command": "y"}}, clients
    )

    async def scenario():
        await manager.start(set())
        rows = {row.key: row for row in manager.status()}
        names = [tool.name for tool in manager.tools()]
        await manager.close()
        return rows, names

    rows, names = asyncio.run(scenario())
    assert rows["good"].state == "connected"
    assert "spawn failed" in rows["bad"].state
    assert names == ["mcp__good__scan"]


def test_missing_required_key_does_not_start_the_server(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan")])
    manager = _manager(
        tmp_path,
        {"burp": {"command": "x", "env": {"BURP_API_KEY": "${BURP_API_KEY}"}}},
        {"burp": client},
    )

    async def scenario():
        await manager.start(set())
        return manager.status()

    (row,) = asyncio.run(scenario())
    assert row.auth == "missing key"
    assert "/auth set BURP_API_KEY" in row.state
    assert not client.entered


def test_disabled_and_unsupported_servers_do_not_connect(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        {
            "off": {"command": "x", "enabled": False},
            "remote": {"type": "http", "url": "https://mcp.example.com"},
        },
        {},
    )

    async def scenario():
        await manager.start(set())
        return {row.key: row.state for row in manager.status()}

    states = asyncio.run(scenario())
    assert states["off"] == "disabled"
    assert "not supported yet" in states["remote"]


def test_disconnect_removes_tools_and_closes_the_client(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan")])
    manager = _manager(tmp_path, {"burp": {"command": "x"}}, {"burp": client})

    async def scenario():
        await manager.start(set())
        disconnected = await manager.disconnect("burp")
        return disconnected, manager.tools(), manager.status()

    disconnected, tools, (row,) = asyncio.run(scenario())
    assert disconnected and tools == []
    assert client.exited
    assert row.state == "disconnected"


def test_cancel_inflight_stops_calls_but_keeps_the_connection(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan")])
    client.gate = asyncio.Event()
    manager = _manager(tmp_path, {"burp": {"command": "x"}}, {"burp": client})

    async def scenario():
        await manager.start(set())
        (tool,) = manager.tools()
        call = asyncio.create_task(tool.execute({"host": "h"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cancelled = manager.cancel_inflight()
        result = await call
        (row,) = manager.status()
        await manager.close()
        return cancelled, result, row

    cancelled, result, row = asyncio.run(scenario())
    assert cancelled == 1
    assert not result.success and "cancelled" in result.error
    assert row.state == "connected"


def test_a_call_times_out_without_closing_the_connection(tmp_path: Path) -> None:
    client = _FakeClient([_tool("scan")])
    client.gate = asyncio.Event()
    manager = _manager(
        tmp_path, {"burp": {"command": "x", "timeout": 0.05}}, {"burp": client}
    )

    async def scenario():
        await manager.start(set())
        (tool,) = manager.tools()
        result = await tool.execute({"host": "h"})
        (row,) = manager.status()
        await manager.close()
        return result, row

    result, row = asyncio.run(scenario())
    assert not result.success and "within 0.05s" in result.error
    assert row.state == "connected"
