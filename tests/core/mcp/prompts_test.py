"""MCP prompts: listing and expansion against a real in-process SDK server."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager
from kerkov.core.mcp.prompts import (
    PromptError,
    describe_prompt,
    expand_prompt,
    parse_prompt_arguments,
)

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.prompts.base import (  # noqa: E402
    AssistantMessage,
    UserMessage,
)
from mcp.types import ImageContent  # noqa: E402

PNG_BASE64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()


def _intel() -> MCPServer:
    server = MCPServer("intel")

    @server.tool()
    def lookup(cve: str) -> str:
        """Look up a CVE."""
        return cve

    @server.prompt(description="Triage one CVE against the scope.")
    def triage(cve: str, depth: str = "quick") -> str:
        return f"Triage {cve} ({depth})."

    @server.prompt(description="A worked example.")
    def example() -> list:
        return [
            UserMessage("How do I check CVE-2026-0001?"),
            AssistantMessage("Start with the vendor advisory."),
        ]

    return server


def _manager(tmp_path: Path, servers: Dict[str, Any]) -> McpManager:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {key: {"command": "x"} for key in servers}}),
        encoding="utf-8",
    )
    return McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: lambda: Client(
            servers[resolved.config.key]
        ),
        environment={},
    )


def _with(manager: McpManager, scenario):
    async def run():
        await manager.start(set())
        try:
            return await scenario()
        finally:
            await manager.close()

    return asyncio.run(run())


def test_lists_prompts_with_required_arguments_marked(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"intel": _intel()})

    listing = _with(manager, manager.list_prompts)

    assert listing.errors == {}
    assert [describe_prompt(p) for p in listing.prompts] == [
        "example(): A worked example.",
        "triage(cve*, depth): Triage one CVE against the scope.",
    ]


def test_expands_a_prompt_with_its_arguments(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"intel": _intel()})

    text = _with(
        manager,
        lambda: expand_prompt(manager, "intel", "triage", {"cve": "CVE-2026-0001"}),
    )

    assert text == "Triage CVE-2026-0001 (quick)."


def test_mixed_roles_are_labelled(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"intel": _intel()})

    text = _with(manager, lambda: expand_prompt(manager, "intel", "example", {}))

    assert text == (
        "[user]\nHow do I check CVE-2026-0001?\n\n"
        "[assistant]\nStart with the vendor advisory."
    )


def test_a_missing_required_argument_is_asked_for(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"intel": _intel()})
    asked = []

    async def ask(argument) -> Optional[str]:
        asked.append(argument.name)
        return "CVE-2026-0002"

    text = _with(
        manager, lambda: expand_prompt(manager, "intel", "triage", {}, ask=ask)
    )

    assert asked == ["cve"]
    assert text == "Triage CVE-2026-0002 (quick)."


@pytest.mark.parametrize(
    "name, arguments, message",
    [
        ("triage", {}, "triage needs cve=<value>"),
        ("triage", {"cve": "x", "severity": "high"}, "takes no argument severity"),
        ("nope", {}, "no prompt named 'nope'"),
    ],
)
def test_bad_requests_say_what_to_fix(tmp_path, name, arguments, message) -> None:
    manager = _manager(tmp_path, {"intel": _intel()})

    async def scenario():
        with pytest.raises(PromptError, match=message):
            await expand_prompt(manager, "intel", name, arguments)

    _with(manager, scenario)


def test_only_servers_with_prompts_list_any(tmp_path: Path) -> None:
    bare = MCPServer("bare")

    @bare.tool()
    def ping() -> str:
        """Pong."""
        return "pong"

    manager = _manager(tmp_path, {"intel": _intel(), "bare": bare})

    async def scenario():
        everything = await manager.list_prompts()
        named = await manager.list_prompts("bare")
        missing = await manager.list_prompts("ghost")
        return everything, named, missing

    everything, named, missing = _with(manager, scenario)
    assert {p.server for p in everything.prompts} == {"intel"}
    assert everything.errors == {}
    assert named.prompts == () and named.errors == {}
    assert missing.errors == {"ghost": "not connected"}


def test_prompt_arguments_parse_name_value_pairs() -> None:
    assert parse_prompt_arguments(["cve=CVE-1", "note=a=b", "empty="]) == {
        "cve": "CVE-1",
        "note": "a=b",
        "empty": "",
    }
    with pytest.raises(PromptError, match="name=value"):
        parse_prompt_arguments(["cve"])


def _screenshot_server() -> MCPServer:
    server = MCPServer("intel")

    @server.prompt(description="Review a screenshot.")
    def review() -> list:
        return [
            UserMessage("Review this login page."),
            UserMessage(
                ImageContent(type="image", data=PNG_BASE64, mime_type="image/png")
            ),
        ]

    return server


def test_attachments_are_saved_to_the_workspace_named_after_loading(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, {"intel": _screenshot_server()})
    workspace = tmp_path / "workspace"
    asked = []

    def start_workspace() -> Path:
        asked.append(True)
        return workspace

    text = _with(
        manager,
        lambda: expand_prompt(
            manager, "intel", "review", {}, workspace=start_workspace
        ),
    )

    [saved] = (workspace / "mcp" / "intel").iterdir()
    assert asked == [True]
    assert saved.read_bytes() == base64.b64decode(PNG_BASE64)
    assert f"saved to {saved}" in text


def test_a_prompt_that_does_not_load_starts_no_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {"intel": _screenshot_server()})

    def start_workspace() -> Path:
        raise AssertionError("workspace requested for a prompt that never loaded")

    async def scenario():
        with pytest.raises(PromptError, match="no prompt named"):
            await expand_prompt(manager, "intel", "nope", {}, workspace=start_workspace)

    _with(manager, scenario)
