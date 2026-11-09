"""`/mcp` parsing, status rendering and config edits through the CLI."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from kerkov.cli.input_parser import InputParser
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.messages.mcp_message import McpMessage
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager
from kerkov.tools.mcp.mcp_tool import McpTool


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def list_tools(self, cursor=None):
        tool = SimpleNamespace(
            name="scan", description="", input_schema={"type": "object"}
        )
        return SimpleNamespace(tools=[tool], next_cursor=None)


def _create_mock_cli(tmp_path: Path, servers=None):
    path = tmp_path / "mcp.json"
    if servers is not None:
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    manager = McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: _FakeClient,
        environment={},
    )
    return SimpleNamespace(
        console=Console(record=True, width=200),
        mcp=manager,
        register_mcp_tools=MagicMock(),
    )


@pytest.mark.parametrize(
    "line,action,server",
    [
        ("/mcp", "status", None),
        ("/mcp status", "status", None),
        ("/mcp list", "list", None),
        ("/mcp list burp", "list", "burp"),
        ("/mcp connect", "connect", None),
        ("/mcp disconnect burp", "disconnect", "burp"),
        ("/mcp remove burp", "remove", "burp"),
        ("/mcp disconnect", "invalid", None),
        ("/mcp frobnicate", "invalid", None),
    ],
)
def test_parses_actions(line: str, action: str, server) -> None:
    message = InputParser.parse_for_message_type(line)

    assert isinstance(message, McpMessage)
    assert (message.action, message.server) == (action, server)


def test_parses_add_with_env_and_command() -> None:
    message = McpMessage.parse(
        '/mcp add burp -e BURP_API_KEY=${BURP_API_KEY} -- npx -y "@example/burp mcp"'
    )

    assert message.action == "add"
    assert message.server == "burp"
    assert message.env == {"BURP_API_KEY": "${BURP_API_KEY}"}
    assert message.command == ["npx", "-y", "@example/burp mcp"]


def test_add_without_a_command_separator_is_invalid() -> None:
    assert McpMessage.parse("/mcp add burp npx").action == "invalid"


def test_status_with_no_servers_explains_how_to_add_one(tmp_path: Path) -> None:
    cli = _create_mock_cli(tmp_path)

    asyncio.run(McpMessage().execute(cli))

    assert "No MCP servers configured" in cli.console.export_text()


def test_add_writes_the_file_connects_and_reregisters(tmp_path: Path) -> None:
    cli = _create_mock_cli(tmp_path)
    message = McpMessage.parse("/mcp add burp -- npx -y burp-mcp")

    async def scenario():
        await message.execute(cli)
        names = [tool.name for tool in cli.mcp.tools()]
        await cli.mcp.close()
        return names

    names = asyncio.run(scenario())
    document = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert document["mcpServers"]["burp"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "burp-mcp"],
    }
    assert names == ["mcp__burp__scan"]
    cli.register_mcp_tools.assert_called()
    assert "prompt cache" in cli.console.export_text()


def test_remove_disconnects_and_edits_the_file(tmp_path: Path) -> None:
    cli = _create_mock_cli(
        tmp_path, {"burp": {"command": "x"}, "keep": {"command": "y"}}
    )

    async def scenario():
        await cli.mcp.start(set())
        await McpMessage("remove", "burp").execute(cli)
        return [row.key for row in cli.mcp.status()]

    keys = asyncio.run(scenario())
    document = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert list(document["mcpServers"]) == ["keep"]
    assert keys == ["keep"]


def test_register_swaps_mcp_tools_in_both_pools(tmp_path: Path) -> None:
    async def caller(*args):
        return None

    def tool(name):
        return McpTool(
            name=name,
            server_key="s",
            original_name=name,
            description="",
            input_schema={"type": "object"},
            caller=caller,
            timeout_seconds=1,
        )

    builtin = SimpleNamespace(name="exec")
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.tools = [builtin, tool("mcp__s__old")]
    cli._master_tools = [builtin, tool("mcp__s__old")]
    pool = cli._master_tools
    cli.mcp = SimpleNamespace(tools=lambda: [tool("mcp__s__new")])

    cli.register_mcp_tools()

    assert [t.name for t in cli.tools] == ["exec", "mcp__s__new"]
    assert [t.name for t in cli._master_tools] == ["exec", "mcp__s__new"]
    assert cli._master_tools is pool
