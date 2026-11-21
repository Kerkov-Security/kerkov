"""McpResourceTool: listing, templates and reads against a real SDK server."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.core.session.approval_rules import allow_rule_choices
from kerkov.tools.mcp.resource_tool import McpResourceTool, TrustedMcpResourceTool

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402


def _server(with_resources: bool = True) -> MCPServer:
    server = MCPServer("notes")

    @server.tool()
    def ping() -> str:
        """Answer pong."""
        return "pong"

    if with_resources:

        @server.resource(
            "notes://scope", name="scope", description="The engagement scope."
        )
        def scope() -> str:
            return "10.0.0.0/24 only"

        @server.resource("notes://logo", name="logo", mime_type="image/png")
        def logo() -> bytes:
            return b"\x89PNG fake"

        @server.resource("notes://hosts/{host}", name="host")
        def host(host: str) -> str:
            return f"{host}: web server"

    return server


def _manager(tmp_path: Path, server: MCPServer, entry: Dict[str, Any]) -> McpManager:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"notes": entry}}), encoding="utf-8")
    return McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: lambda: Client(server),
        environment={},
    )


def _run(manager: McpManager, *calls: Dict[str, Any]):
    async def scenario():
        await manager.start(set())
        tools = {tool.name: tool for tool in manager.tools()}
        resource_tool = tools.get("mcp__notes__resources")
        results = [await resource_tool.execute(call) for call in calls]
        rows = manager.status()
        await manager.close()
        return tools, results, rows

    return asyncio.run(scenario())


def test_lists_reads_and_fills_templates(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _server(), {"command": "x"})

    tools, (listed, templates, read, filled), (row,) = _run(
        manager,
        {"action": "list"},
        {"action": "templates"},
        {"action": "read", "uri": "notes://scope"},
        {"action": "read", "uri": "notes://hosts/10.0.0.5"},
    )

    assert sorted(tools) == ["mcp__notes__ping", "mcp__notes__resources"]
    assert isinstance(tools["mcp__notes__resources"], McpResourceTool)
    assert "notes://scope (scope, text/plain): The engagement scope." in listed.output
    assert "notes://hosts/{host} (host" in templates.output
    assert read.output == "[resource: notes://scope]\n10.0.0.0/24 only"
    assert filled.output.endswith("10.0.0.5: web server")
    assert row.resources == "mcp__notes__resources"
    assert (row.advertised, row.exposed) == (1, 1)


def test_binary_contents_go_to_the_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _server(), {"command": "x"})
    token = set_current_workspace(tmp_path / "workspace")
    try:
        _, (read,), _ = _run(manager, {"action": "read", "uri": "notes://logo"})
    finally:
        reset_current_workspace(token)

    (saved,) = read.metadata["files"]
    assert Path(saved).read_bytes() == b"\x89PNG fake"
    assert "base64" not in read.output and saved in read.output


def test_a_server_with_nothing_to_read_gets_no_resource_tool(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _server(with_resources=False), {"command": "x"})

    async def scenario():
        await manager.start(set())
        names = [tool.name for tool in manager.tools()]
        await manager.close()
        return names

    assert asyncio.run(scenario()) == ["mcp__notes__ping"]


def test_resources_false_withholds_the_tool(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _server(), {"command": "x", "resources": False})

    async def scenario():
        await manager.start(set())
        names = [tool.name for tool in manager.tools()]
        await manager.close()
        return names

    assert asyncio.run(scenario()) == ["mcp__notes__ping"]


def test_a_server_tool_named_resources_cannot_take_the_resource_tools_name(
    tmp_path: Path,
) -> None:
    server = _server()

    @server.tool()
    def resources() -> str:
        """A tool that happens to share the name."""
        return "mine"

    manager = _manager(tmp_path, server, {"command": "x"})

    async def scenario():
        await manager.start(set())
        tools = {tool.name: tool.original_name for tool in manager.tools()}
        await manager.close()
        return tools

    tools = asyncio.run(scenario())
    assert tools["mcp__notes__resources"] == "resources"
    renamed = [name for name, original in tools.items() if original == "resources"]
    assert len(renamed) == 2


def test_an_unknown_resource_is_a_failure_the_model_can_read(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _server(), {"command": "x"})

    _, (missing,), _ = _run(manager, {"action": "read", "uri": "notes://nope"})

    assert not missing.success
    assert "notes://nope" in missing.error


@pytest.mark.parametrize(
    "params, problem",
    [
        ({"action": "read"}, "read needs a uri"),
        ({"action": "list", "uri": "notes://x"}, "takes no uri"),
        ({"action": "read", "uri": "notes://x", "cursor": "2"}, "no cursor"),
    ],
)
def test_arguments_are_checked_before_the_server_is_asked(params, problem) -> None:
    async def never(*_args):
        raise AssertionError("the server must not be asked")

    tool = McpResourceTool(
        name="mcp__notes__resources",
        server_key="notes",
        request=never,
        timeout_seconds=5,
    )

    result = asyncio.run(tool.execute(params))

    assert not result.success and problem in result.error


def test_trust_decides_whether_reads_prompt_and_can_be_saved(tmp_path: Path) -> None:
    trusted = _manager(tmp_path, _server(), {"command": "x", "trusted": True})

    tools, _, _ = _run(trusted, {"action": "list"})
    tool = tools["mcp__notes__resources"]
    plain = McpResourceTool(
        name="mcp__notes__resources",
        server_key="notes",
        request=None,
        timeout_seconds=1,
    )

    assert isinstance(tool, TrustedMcpResourceTool)
    assert tool.prompts_every_call is False and plain.prompts_every_call is True
    assert [c.rule_string for c in allow_rule_choices(tool, {"action": "list"})] == [
        "mcp__notes__resources({})",
        "mcp__notes__resources({'action': 'list'})",
    ]
    assert allow_rule_choices(plain, {"action": "list"}) == []


def test_web_list_shows_the_resource_tool(tmp_path: Path) -> None:
    from kerkov.web.runtime.mcp import run_mcp_command

    manager = _manager(tmp_path, _server(), {"command": "x"})
    events = []
    session = SimpleNamespace(
        mcp=manager, bus=SimpleNamespace(emit=lambda *event: events.append(event))
    )

    async def scenario():
        await manager.start(set())
        await run_mcp_command(session, "/mcp list")
        await manager.close()

    asyncio.run(scenario())
    ((_, payload),) = events
    assert "  mcp__notes__resources ← resources (list, read)" in payload["detail"]
