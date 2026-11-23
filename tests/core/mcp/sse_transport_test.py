"""The legacy HTTP+SSE transport against a real SDK server on a loopback port.

An in-process ASGI transport buffers a whole response before returning it, so it
can never deliver an SSE stream that stays open; this server listens for real.
"""

import asyncio
import json
import socket
from pathlib import Path
from typing import Dict, Optional

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager

pytest.importorskip("mcp")

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402


class _GuardedSseServer:
    """An SSE MCP server that answers 401 unless the expected bearer is sent."""

    def __init__(self, token: Optional[str]) -> None:
        self.token = token
        self.seen_headers: Dict[str, str] = {}
        server = MCPServer("legacy")

        @server.tool()
        def lookup(cve: str) -> str:
            """Look up a CVE."""
            return f"{cve}: critical"

        self.app = server.sse_app()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            self.seen_headers = headers
            expected = f"Bearer {self.token}" if self.token else None
            if expected and headers.get("authorization") != expected:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b'Basic realm="x"')],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run(tmp_path: Path, guarded: _GuardedSseServer, secrets, call: bool = False):
    port = _free_port()
    path = tmp_path / "mcp.json"
    entry = {
        "type": "sse",
        "url": f"http://127.0.0.1:{port}/sse",
        "headers": {"Authorization": "Bearer ${LEGACY_KEY}"},
    }
    path.write_text(json.dumps({"mcpServers": {"legacy": entry}}), encoding="utf-8")
    manager = McpManager(
        path,
        credentials=CredentialManager(
            environment=secrets, persistent_store=MemorySecretStore()
        ),
        environment={},
    )

    async def scenario():
        server = uvicorn.Server(
            uvicorn.Config(guarded, host="127.0.0.1", port=port, log_level="error")
        )
        serving = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            await manager.start(set())
            (row,) = manager.status()
            result = None
            if call and manager.tools():
                (tool,) = manager.tools()
                result = await tool.execute({"cve": "CVE-2026-0001"})
            await manager.close()
            return row, result
        finally:
            server.should_exit = True
            await serving

    return asyncio.run(asyncio.wait_for(scenario(), 30))


def test_an_sse_server_connects_with_its_header_key_and_runs_a_tool(
    tmp_path: Path,
) -> None:
    guarded = _GuardedSseServer("good")

    row, result = _run(tmp_path, guarded, {"LEGACY_KEY": "good"}, call=True)

    assert row.transport == "sse"
    assert row.state == "connected"
    assert row.auth == "key (environment)"
    assert row.tools == {"mcp__legacy__lookup": "lookup"}
    assert result.success and result.output == "CVE-2026-0001: critical"


def test_an_sse_server_that_refuses_the_key_reads_as_rejected(tmp_path: Path) -> None:
    guarded = _GuardedSseServer("good")

    row, _ = _run(tmp_path, guarded, {"LEGACY_KEY": "wrong"})

    assert row.auth == "rejected"
    assert "HTTP 401" in row.state
