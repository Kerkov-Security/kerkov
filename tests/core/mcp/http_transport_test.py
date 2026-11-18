"""Streamable HTTP against a real SDK server app, served in-process over ASGI."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Optional

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.manager import McpManager

pytest.importorskip("mcp")

import httpx2  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

_URL = "http://127.0.0.1:8000/mcp"


class _GuardedServer:
    """An MCP server that answers 401 unless the expected bearer token is sent."""

    def __init__(self, token: Optional[str], challenge: bytes = b"Bearer") -> None:
        self.token = token
        self.challenge = challenge
        self.seen_headers: Dict[str, str] = {}
        self.server = MCPServer("intel")

        @self.server.tool()
        def lookup(cve: str) -> str:
            """Look up a CVE."""
            return f"{cve}: critical"

        self.app = self.server.streamable_http_app()

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
                        "headers": [(b"www-authenticate", self.challenge)],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


class _NotFound:
    """Answers 404 off the MCP path, as a server with a wrong `url` would."""

    def __init__(self, guarded: _GuardedServer) -> None:
        self.guarded = guarded

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] != "/mcp":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"not found"})
            return
        await self.guarded(scope, receive, send)


def _manager(tmp_path: Path, entry, guarded, secrets=None):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"intel": entry}}), encoding="utf-8")
    return McpManager(
        path,
        credentials=CredentialManager(
            environment=secrets or {}, persistent_store=MemorySecretStore()
        ),
        http_client_factory=lambda headers: httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=guarded), headers=headers
        ),
        environment={},
    )


def _run(manager: McpManager, guarded: _GuardedServer, call: bool = False):
    async def scenario():
        async with guarded.server.session_manager.run():
            await manager.start(set())
            (row,) = manager.status()
            result = None
            if call and manager.tools():
                (tool,) = manager.tools()
                result = await tool.execute({"cve": "CVE-2026-0001"})
            await manager.close()
            return row, result

    return asyncio.run(scenario())


def test_header_key_is_resolved_sent_and_the_tool_works(tmp_path: Path) -> None:
    guarded = _GuardedServer("good")
    manager = _manager(
        tmp_path,
        {
            "type": "http",
            "url": _URL,
            "headers": {"Authorization": "Bearer ${INTEL_KEY}"},
        },
        guarded,
        secrets={"INTEL_KEY": "good"},
    )

    row, result = _run(manager, guarded, call=True)

    assert row.state == "connected"
    assert row.auth == "key (environment)"
    assert result.success and result.output == "CVE-2026-0001: critical"


def test_a_refused_key_shows_rejected(tmp_path: Path) -> None:
    guarded = _GuardedServer("good")
    manager = _manager(
        tmp_path,
        {
            "type": "http",
            "url": _URL,
            "headers": {"Authorization": "Bearer ${INTEL_KEY}"},
        },
        guarded,
        secrets={"INTEL_KEY": "wrong"},
    )

    row, _ = _run(manager, guarded)

    assert row.auth == "rejected"
    assert "HTTP 401" in row.state


def test_an_absent_optional_key_drops_the_header(tmp_path: Path) -> None:
    guarded = _GuardedServer(None)
    manager = _manager(
        tmp_path,
        {
            "type": "http",
            "url": _URL,
            "headers": {"Authorization": "Bearer ${INTEL_KEY}"},
            "optional": ["INTEL_KEY"],
        },
        guarded,
    )

    row, _ = _run(manager, guarded)

    assert row.state == "connected"
    assert row.auth == "no key (optional)"
    assert "authorization" not in guarded.seen_headers


def test_a_bearer_challenge_without_a_key_asks_for_login(tmp_path: Path) -> None:
    guarded = _GuardedServer("good")
    manager = _manager(tmp_path, {"type": "http", "url": _URL}, guarded)

    row, _ = _run(manager, guarded)

    assert row.auth == "oauth: needs login"
    assert "/mcp login intel" in row.state


def test_a_non_bearer_challenge_without_a_key_needs_credentials(tmp_path: Path) -> None:
    guarded = _GuardedServer("good", challenge=b'Basic realm="intel"')
    manager = _manager(tmp_path, {"type": "http", "url": _URL}, guarded)

    row, _ = _run(manager, guarded)

    assert row.auth == "credentials required"
    assert "HTTP 401" in row.state


def test_a_literal_header_the_server_refuses_reads_as_rejected(tmp_path: Path) -> None:
    guarded = _GuardedServer("good")
    manager = _manager(
        tmp_path,
        {"type": "http", "url": _URL, "headers": {"Authorization": "Bearer wrong"}},
        guarded,
    )

    row, _ = _run(manager, guarded)

    assert row.auth == "rejected"
    assert "HTTP 401" in row.state


def test_a_key_in_the_url_stays_out_of_the_request_log(tmp_path: Path, caplog) -> None:
    guarded = _GuardedServer(None)
    manager = _manager(
        tmp_path,
        {"type": "http", "url": f"{_URL}?token=${{INTEL_KEY}}"},
        guarded,
        secrets={"INTEL_KEY": "s3cret-token"},
    )

    with caplog.at_level(logging.INFO, logger="httpx2"):
        row, _ = _run(manager, guarded)

    assert row.state == "connected"
    logged = [r.getMessage() for r in caplog.records if r.name == "httpx2"]
    assert logged and all("s3cret-token" not in message for message in logged)
    assert all("/<redacted>" in message for message in logged)


def test_a_key_in_the_url_path_stays_out_of_the_logs_and_the_error(
    tmp_path: Path, caplog
) -> None:
    guarded = _GuardedServer(None)
    manager = _manager(
        tmp_path,
        {"type": "http", "url": f"{_URL}/${{INTEL_KEY}}"},
        _NotFound(guarded),
        secrets={"INTEL_KEY": "s3cret-path"},
    )

    with caplog.at_level(logging.INFO, logger="httpx2"):
        row, _ = _run(manager, guarded)

    assert row.state.startswith("failed:")
    assert "s3cret-path" not in row.state
    logged = [r.getMessage() for r in caplog.records if r.name == "httpx2"]
    assert logged and all("s3cret-path" not in message for message in logged)
    assert all("/<redacted>" in message for message in logged)


@pytest.mark.parametrize(
    "url, secrets, key",
    [
        ("http://${INTEL_KEY}.example.com/mcp", {"INTEL_KEY": "MiXeD-TOKEN"}, "mixed"),
        ("http://${INTEL_KEY:-FALLBACK-TOKEN}.example.com/mcp", {}, "fallback"),
    ],
    ids=["normalized-host", "fallback-default"],
)
def test_a_key_in_the_url_host_stays_out_of_the_logs_and_the_error(
    tmp_path: Path, caplog, url: str, secrets: Dict[str, str], key: str
) -> None:
    guarded = _GuardedServer(None)
    manager = _manager(tmp_path, {"type": "http", "url": url}, guarded, secrets=secrets)

    with caplog.at_level(logging.INFO, logger="httpx2"):
        row, _ = _run(manager, guarded)

    logged = [r.getMessage() for r in caplog.records if r.name == "httpx2"]
    assert logged
    for text in [row.state, *logged]:
        assert key not in text.lower()
        assert "token" not in text.lower()
    assert all("${INTEL_KEY}.example.com" in message for message in logged)
