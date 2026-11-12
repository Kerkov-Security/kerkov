"""`/mcp` parsing, status rendering and config edits through the CLI."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from kerkov.cli.input_parser import InputParser
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.messages.mcp_message import McpMessage
from kerkov.core.credentials import (
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.core.mcp.manager import McpManager
from kerkov.core.mcp.oauth import CredentialTokenStorage
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
        ("/mcp login intel", "login", "intel"),
        ("/mcp logout intel", "logout", "intel"),
        ("/mcp login", "invalid", None),
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
    assert message.entry == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@example/burp mcp"],
        "env": {"BURP_API_KEY": "${BURP_API_KEY}"},
    }


def test_parses_add_for_an_http_server_with_headers() -> None:
    message = McpMessage.parse(
        "/mcp add intel --url https://mcp.example.com/mcp "
        "-H 'Authorization: Bearer ${INTEL_KEY}'"
    )

    assert message.entry == {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer ${INTEL_KEY}"},
    }


@pytest.mark.parametrize(
    "line",
    [
        "/mcp add burp npx",
        "/mcp add burp --url https://x -- npx",
        "/mcp add burp -H 'A: b' -- npx",
        "/mcp add burp -e A=b --url https://x",
    ],
)
def test_ambiguous_or_mismatched_add_is_invalid(line: str) -> None:
    assert McpMessage.parse(line).action == "invalid"


def test_parses_key_with_and_without_a_name() -> None:
    assert (McpMessage.parse("/mcp key intel").key_name, "key") == (None, "key")
    named = McpMessage.parse("/mcp key intel INTEL_KEY")
    assert (named.action, named.server, named.key_name) == (
        "key",
        "intel",
        "INTEL_KEY",
    )


def test_key_prompts_saves_and_reconnects(tmp_path: Path) -> None:
    cli = _create_mock_cli(
        tmp_path,
        {"intel": {"command": "x", "env": {"INTEL_KEY": "${INTEL_KEY}"}}},
    )
    cli.credentials = CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    cli.mcp._credentials = cli.credentials
    cli.prompt_for_credential = AsyncMock(
        return_value=CredentialInput(value="s3cret", persist=False)
    )

    async def scenario():
        await cli.mcp.start(set())
        before = cli.mcp.status()[0].auth
        await McpMessage.parse("/mcp key intel").execute(cli)
        after = cli.mcp.status()[0]
        await cli.mcp.close()
        return before, after

    before, after = asyncio.run(scenario())
    assert before == "missing key"
    assert after.state == "connected"
    assert after.auth == "key (session)"
    assert "s3cret" not in cli.console.export_text()


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


def test_logout_reports_a_login_it_could_not_remove(tmp_path: Path) -> None:
    # Only these two cases need the optional SDK, for its token model.
    pytest.importorskip("mcp")
    from mcp.shared.auth import OAuthToken

    class _StuckStore(MemorySecretStore):
        def delete(self, key: str) -> None:
            raise OSError("the keyring is locked")

    cli = _create_mock_cli(
        tmp_path, {"intel": {"type": "http", "url": "https://mcp.example.com/mcp"}}
    )
    credentials = CredentialManager(environment={}, persistent_store=_StuckStore())
    cli.mcp._credentials = credentials
    cli.mcp.load()
    storage = CredentialTokenStorage(
        credentials, cli.mcp.server_config("intel"), "https://mcp.example.com/mcp"
    )

    async def scenario():
        await storage.set_tokens(OAuthToken(access_token="a", expires_in=3600))
        await McpMessage("logout", "intel").execute(cli)

    asyncio.run(scenario())

    output = cli.console.export_text()
    assert "could not be confirmed as removed" in output
    assert "✓" not in output
    assert storage.stored().logged_in


def test_login_refuses_a_plain_http_server(tmp_path: Path) -> None:
    cli = _create_mock_cli(
        tmp_path, {"intel": {"type": "http", "url": "http://mcp.example.com/mcp"}}
    )

    asyncio.run(McpMessage("login", "intel").execute(cli))

    assert "https" in cli.console.export_text()
    assert cli.mcp.status()[0].state == "not started"


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
