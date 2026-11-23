"""`/mcp` parsing, status rendering and config edits through the CLI."""

import asyncio
import base64
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
    server_capabilities = SimpleNamespace(tools=SimpleNamespace())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def list_tools(self, cursor=None):
        tool = SimpleNamespace(
            name="scan", description="", input_schema={"type": "object"}
        )
        return SimpleNamespace(tools=[tool], next_cursor=None)


def _create_mock_cli(tmp_path: Path, servers=None, credentials=None, width: int = 200):
    path = tmp_path / "mcp.json"
    if servers is not None:
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    credentials = credentials or CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    manager = McpManager(
        path,
        credentials=credentials,
        client_factory_provider=lambda resolved: _FakeClient,
        environment={},
    )
    return SimpleNamespace(
        console=Console(record=True, width=width),
        credentials=credentials,
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


def test_parses_add_for_a_legacy_sse_server() -> None:
    message = McpMessage.parse(
        "/mcp add intel --url https://mcp.example.com/sse --transport sse"
    )

    assert message.entry == {"type": "sse", "url": "https://mcp.example.com/sse"}


@pytest.mark.parametrize(
    "line",
    [
        "/mcp add burp npx",
        "/mcp add burp --url https://x -- npx",
        "/mcp add burp -H 'A: b' -- npx",
        "/mcp add burp -e A=b --url https://x",
        "/mcp add burp --transport sse -- npx",
        "/mcp add burp --url https://x --transport stdio",
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


def test_status_bounds_the_table_on_a_wide_terminal(tmp_path: Path) -> None:
    cli = _create_mock_cli(
        tmp_path,
        {"demo": {"command": "server", "env": {"KEY": "${ABSENT_KEY}"}}},
        width=200,
    )

    async def run() -> None:
        cli.mcp.load()
        await cli.mcp.connect("demo")
        await McpMessage().execute(cli)

    asyncio.run(run())

    output = cli.console.export_text()
    assert "ABSENT_KEY" in output
    # Only the table: the `Config:` line carries a tmp_path of incidental width.
    borders = ("┌", "│", "├", "└")
    table = [line for line in output.splitlines() if line.startswith(borders)]
    assert table
    assert max(len(line) for line in table) <= 110


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


def test_parses_prompts_and_prompt_with_arguments() -> None:
    listing = McpMessage.parse("/mcp prompts intel")
    prompt = McpMessage.parse('/mcp prompt intel triage cve=CVE-1 "note=two words"')

    assert (listing.action, listing.server) == ("prompts", "intel")
    assert (prompt.action, prompt.server, prompt.prompt) == (
        "prompt",
        "intel",
        "triage",
    )
    assert prompt.prompt_arguments == {"cve": "CVE-1", "note": "two words"}
    assert McpMessage.parse("/mcp prompt intel").action == "invalid"
    assert McpMessage.parse("/mcp prompt intel triage cve").action == "invalid"


PNG = b"\x89PNG\r\n\x1a\n"


def _prompt_cli(tmp_path: Path, answers, *, provider: bool = True):
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.prompts.base import UserMessage
    from mcp.types import ImageContent

    server = MCPServer("intel")

    @server.prompt(description="Triage one CVE.")
    def triage(cve: str) -> str:
        return f"Triage {cve}."

    @server.prompt(description="Review a screenshot.")
    def review() -> list:
        data = base64.b64encode(PNG).decode()
        return [
            UserMessage(ImageContent(type="image", data=data, mime_type="image/png"))
        ]

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"intel": {"command": "x"}}}))
    cli = _create_mock_cli(tmp_path)
    cli.mcp = McpManager(
        path,
        credentials=cli.credentials,
        client_factory_provider=lambda resolved: lambda: Client(server),
        environment={},
    )
    cli.get_response = AsyncMock()
    cli.require_provider = MagicMock(return_value=provider)
    cli.engagement_workspace = MagicMock(return_value=tmp_path / "workspace")
    cli.input_manager = MagicMock(is_watching_for_interrupt=False)
    cli.input_manager.get_text = AsyncMock(side_effect=answers)
    return cli


def _run_cli(cli, *lines: str) -> None:
    async def scenario():
        await cli.mcp.start(set())
        try:
            for line in lines:
                await McpMessage.parse(line).execute(cli)
        finally:
            await cli.mcp.close()

    asyncio.run(scenario())


def test_prompts_lists_and_prompt_sends_the_expanded_text(tmp_path: Path) -> None:
    cli = _prompt_cli(tmp_path, [])

    _run_cli(cli, "/mcp prompts", "/mcp prompt intel triage cve=CVE-2026-0001")

    output = cli.console.export_text()
    assert "triage(cve*): Triage one CVE." in output
    cli.get_response.assert_awaited_once_with("Triage CVE-2026-0001.")


def test_prompt_asks_for_a_missing_argument(tmp_path: Path) -> None:
    cli = _prompt_cli(tmp_path, ["CVE-2026-0009"])

    _run_cli(cli, "/mcp prompt intel triage")

    cli.get_response.assert_awaited_once_with("Triage CVE-2026-0009.")


def test_a_cancelled_argument_sends_nothing(tmp_path: Path) -> None:
    cli = _prompt_cli(tmp_path, [""])

    _run_cli(cli, "/mcp prompt intel triage")

    cli.get_response.assert_not_awaited()
    assert "triage needs cve=<value>" in cli.console.export_text()
    cli.engagement_workspace.assert_not_called()


def test_prompt_attachments_go_to_the_engagement_workspace(tmp_path: Path) -> None:
    cli = _prompt_cli(tmp_path, [])

    _run_cli(cli, "/mcp prompt intel review")

    [saved] = (tmp_path / "workspace" / "mcp" / "intel").iterdir()
    assert saved.read_bytes() == PNG
    cli.engagement_workspace.assert_called_once_with()
    [text] = cli.get_response.await_args.args
    assert f"saved to {saved}" in text


def test_prompt_without_a_provider_loads_nothing(tmp_path: Path) -> None:
    cli = _prompt_cli(tmp_path, ["CVE-2026-0009"], provider=False)

    _run_cli(cli, "/mcp prompt intel triage")

    cli.input_manager.get_text.assert_not_awaited()
    cli.engagement_workspace.assert_not_called()
    cli.get_response.assert_not_awaited()


def test_engagement_workspace_starts_the_engagement_first(tmp_path: Path) -> None:
    from kerkov.core.run_workspace import RunWorkspaceManager

    manager = RunWorkspaceManager(tmp_path)
    cli = SimpleNamespace(
        _apply_system_prompt_if_possible=lambda: manager.activate("eng-1")
    )

    async def scenario():
        return KerkovCLI.engagement_workspace(cli)

    assert asyncio.run(scenario()) == manager.path_for_engagement("eng-1")
