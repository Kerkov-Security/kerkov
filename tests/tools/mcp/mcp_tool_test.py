"""McpTool: schema pass-through, validation, approval breadth and results."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from kerkov.core.mcp.connection import McpCallError
from kerkov.models.basic_models import Message
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.tools.mcp.mcp_tool import McpTool, SchemaNotRepresentableError
from kerkov.tools.mcp.results import adapt_call_result

_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "definitions": {"Port": {"type": "integer", "minimum": 1, "maximum": 65535}},
    "properties": {
        "host": {"type": "string", "title": "Host"},
        "port": {"$ref": "#/definitions/Port"},
        "token": {"type": "string"},
    },
    "required": ["host"],
}


def _tool(caller=None, schema=None, name="mcp__srv__scan") -> McpTool:
    async def default_caller(server, tool, arguments, timeout):
        return _result([_text(f"{tool}:{json.dumps(arguments, sort_keys=True)}")])

    return McpTool(
        name=name,
        server_key="srv",
        original_name="scan",
        description="Scan a host.",
        input_schema=schema or _SCHEMA,
        caller=caller or default_caller,
        timeout_seconds=5,
    )


def _text(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def _result(content: List[Any], structured: Any = None, is_error: bool = False) -> Any:
    return SimpleNamespace(
        content=content, structured_content=structured, is_error=is_error
    )


def test_advertises_the_normalized_server_schema() -> None:
    schema = _tool().get_generic_schema()

    assert schema.name == "mcp__srv__scan"
    assert schema.description.startswith("[MCP server 'srv' tool 'scan'] Scan")
    assert schema.parameters["properties"]["port"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 65535,
    }
    assert "definitions" not in schema.parameters


def test_validates_against_the_original_schema_before_calling() -> None:
    calls: List[Dict[str, Any]] = []

    async def caller(server, tool, arguments, timeout):
        calls.append(arguments)
        return _result([_text("ok")])

    tool = _tool(caller)

    async def scenario():
        return (
            await tool.execute({"host": "h", "port": 70000}),
            await tool.execute({"host": "h", "port": 22}),
        )

    rejected, accepted = asyncio.run(scenario())
    assert not rejected.success and "port" in rejected.error
    assert accepted.success
    assert calls == [{"host": "h", "port": 22}]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"x": {"$ref": "#/$defs/A/properties/b"}}},
        {"type": "object", "properties": {"x": {"type": 7}}},
    ],
)
def test_unrepresentable_or_invalid_schemas_are_refused(schema) -> None:
    with pytest.raises(SchemaNotRepresentableError):
        _tool(schema=schema)


def test_approvals_can_never_be_saved_as_rules() -> None:
    scope = McpTool.approval_scope(McpTool, {"host": "h", "token": "secret"})

    assert scope.saveable is False


def test_logs_argument_names_but_never_values() -> None:
    assert _tool().loggable_params({"token": "secret", "host": "h"}) == [
        "host",
        "token",
    ]


def test_connection_errors_become_failed_results() -> None:
    async def caller(server, tool, arguments, timeout):
        raise McpCallError("MCP server srv is not connected")

    result = asyncio.run(_tool(caller).execute({"host": "h"}))

    assert not result.success
    assert result.error == "MCP server srv is not connected"


def test_tool_errors_are_failures_the_model_can_read() -> None:
    result = adapt_call_result(
        _result([_text("host unreachable")], is_error=True),
        server_key="srv",
        tool_name="scan",
        call_id="c1",
    )

    assert not result.success
    assert result.error == "host unreachable"


def test_structured_content_is_used_only_without_text() -> None:
    mirrored = adapt_call_result(
        _result([_text('{"open": [22]}')], structured={"open": [22]}),
        server_key="srv",
        tool_name="scan",
        call_id=None,
    )
    structured_only = adapt_call_result(
        _result([], structured={"open": [22]}),
        server_key="srv",
        tool_name="scan",
        call_id=None,
    )

    assert mirrored.output == '{"open": [22]}'
    assert json.loads(structured_only.output) == {"open": [22]}


def test_binary_content_is_saved_to_the_workspace_not_history(tmp_path: Path) -> None:
    png = base64.b64encode(b"\x89PNG fake").decode()
    result = adapt_call_result(
        _result(
            [
                _text("screenshot:"),
                SimpleNamespace(type="image", data=png, mime_type="image/png"),
                SimpleNamespace(
                    type="resource",
                    resource=SimpleNamespace(
                        uri="file:///report.txt", mime_type="text/plain", text="body"
                    ),
                ),
                SimpleNamespace(
                    type="resource_link",
                    uri="file:///big.bin",
                    name="big",
                    description=None,
                ),
            ]
        ),
        server_key="srv",
        tool_name="shot",
        call_id="toolu/01",
        workspace=tmp_path,
    )

    saved = tmp_path / "mcp" / "srv" / "toolu_01-1.png"
    assert saved.read_bytes() == b"\x89PNG fake"
    assert png not in result.output
    assert str(saved) in result.output
    assert "[resource: file:///report.txt]\nbody" in result.output
    assert "[resource link: file:///big.bin (big)]" in result.output
    assert result.metadata["files"] == [str(saved)]


def test_binary_content_without_a_workspace_is_described_not_inlined() -> None:
    data = base64.b64encode(b"abc").decode()
    result = adapt_call_result(
        _result([SimpleNamespace(type="audio", data=data, mime_type="audio/wav")]),
        server_key="srv",
        tool_name="rec",
        call_id=None,
        workspace=None,
    )

    assert data not in result.output
    assert "3 bytes omitted" in result.output


def test_anthropic_prefix_is_byte_stable_with_mcp_tools() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-test")
    messages = [
        Message(role="system", content="STABLE"),
        Message(role="user", content="hi"),
    ]

    def build():
        tools = [_tool(name="mcp__srv__b"), _tool(name="mcp__srv__a")]
        return provider._build_request(messages, None, None, tools)

    first, second = build(), build()

    assert json.dumps(first["tools"]) == json.dumps(second["tools"])
    assert json.dumps(first["system"]) == json.dumps(second["system"])


def test_round_trip_against_a_real_sdk_server(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    from kerkov.core.credentials import CredentialManager, MemorySecretStore
    from kerkov.core.mcp.manager import McpManager

    server = MCPServer("demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "x"}}}))
    manager = McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: lambda: Client(server),
        environment={},
    )

    async def scenario():
        await manager.start(set())
        tools = {tool.original_name: tool for tool in manager.tools()}
        added = await tools["add"].execute({"a": 2, "b": 3})
        failed = await tools["boom"].execute({})
        await manager.close()
        return sorted(tools), added, failed

    names, added, failed = asyncio.run(scenario())
    assert names == ["add", "boom"]
    assert added.success and added.output == "5"
    assert not failed.success
