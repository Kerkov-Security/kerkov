"""OAuth for HTTP MCP servers: storage, the loopback login and connect-time rules."""

import asyncio
import json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.config import parse_server
from kerkov.core.mcp.manager import McpManager
from kerkov.core.mcp.oauth import (
    CredentialTokenStorage,
    LoginCancelled,
    LoopbackAuthorization,
    free_loopback_port,
)

pytest.importorskip("mcp")

import httpx  # noqa: E402
import httpx2  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken  # noqa: E402

_BASE = "http://127.0.0.1:8000"
_URL = f"{_BASE}/mcp"


def _credentials() -> CredentialManager:
    return CredentialManager(environment={}, persistent_store=MemorySecretStore())


def _server(entry: Optional[Dict[str, Any]] = None):
    config, _ = parse_server("intel", {"type": "http", "url": _URL, **(entry or {})})
    return config


class _FakeOAuthServer:
    """An MCP resource server plus a minimal authorization server, over ASGI."""

    def __init__(self) -> None:
        self.issued: List[str] = []
        self.registrations = 0
        self.refreshes = 0
        self.mcp = MCPServer("intel")

        @self.mcp.tool()
        def lookup(cve: str) -> str:
            """Look up a CVE."""
            return f"{cve}: critical"

        self.app = self.mcp.streamable_http_app()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/.well-known/oauth-protected-resource"):
            await _json(
                send,
                {"resource": _URL, "authorization_servers": [_BASE]},
            )
        elif path.startswith("/.well-known/oauth-authorization-server"):
            await _json(
                send,
                {
                    "issuer": _BASE,
                    "authorization_endpoint": f"{_BASE}/authorize",
                    "token_endpoint": f"{_BASE}/token",
                    "registration_endpoint": f"{_BASE}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        elif path == "/register":
            body = json.loads(await _body(receive))
            self.registrations += 1
            await _json(
                send,
                {**body, "client_id": f"client-{self.registrations}"},
                status=201,
            )
        elif path == "/token":
            form = urllib.parse.parse_qs((await _body(receive)).decode())
            if form["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
            token = f"tok-{len(self.issued) + 1}"
            self.issued.append(token)
            await _json(
                send,
                {
                    "access_token": token,
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-1",
                },
            )
        else:
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            supplied = headers.get("authorization", "").removeprefix("Bearer ")
            if supplied not in self.issued:
                challenge = (
                    f'Bearer resource_metadata="{_BASE}/.well-known/'
                    'oauth-protected-resource/mcp"'
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", challenge.encode())],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            await self.app(scope, receive, send)


async def _body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def _json(send, payload, status: int = 200) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _manager(tmp_path: Path, fake: _FakeOAuthServer, credentials: CredentialManager):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"intel": {"type": "http", "url": _URL}}}),
        encoding="utf-8",
    )
    return McpManager(
        path,
        credentials=credentials,
        http_client_factory=lambda headers: httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=fake), headers=headers
        ),
        environment={},
    )


def _browser(code: str = "the-code"):
    """Stands in for the operator's browser: follows the redirect back to Kerkov."""
    opened: List[str] = []

    def open_browser(authorization_url: str) -> None:
        opened.append(authorization_url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        callback = f"{query['redirect_uri'][0]}?code={code}&state={query['state'][0]}"

        def follow() -> None:
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    httpx.get(callback, timeout=2)
                    return
                except httpx.HTTPError:
                    time.sleep(0.05)

        threading.Thread(target=follow, daemon=True).start()

    return open_browser, opened


def _login(open_browser, confirm=True, origins=None) -> LoopbackAuthorization:
    async def confirm_origin(origin: str) -> bool:
        if origins is not None:
            origins.append(origin)
        return confirm

    async def paste_callback() -> Optional[str]:
        return None

    return LoopbackAuthorization(
        free_loopback_port(),
        confirm_origin=confirm_origin,
        paste_callback=paste_callback,
        notify=lambda text: None,
        open_browser=open_browser,
    )


def test_storage_round_trips_and_is_bound_to_the_server_url() -> None:
    credentials = _credentials()
    storage = CredentialTokenStorage(credentials, _server(), _URL)

    async def scenario():
        await storage.set_tokens(
            OAuthToken(access_token="a", expires_in=60, refresh_token="r")
        )
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id="c", redirect_uris=["http://127.0.0.1:4567/callback"]
            )
        )
        moved = CredentialTokenStorage(credentials, _server(), f"{_BASE}/other")
        return (
            await storage.get_tokens(),
            await moved.get_tokens(),
            storage.redirect_port(),
        )

    tokens, moved_tokens, port = asyncio.run(scenario())
    assert tokens.access_token == "a"
    assert moved_tokens is None
    assert port == 4567
    assert storage.stored().logged_in and not storage.stored().expired

    storage.clear()
    assert not storage.stored().logged_in


def test_expired_token_without_refresh_is_reported() -> None:
    storage = CredentialTokenStorage(_credentials(), _server(), _URL)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", expires_in=-1)))

    stored = storage.stored()

    assert stored.expired and not stored.refreshable


def test_a_preregistered_client_skips_registration() -> None:
    config = _server({"oauth": {"clientId": "pre", "callbackPort": 4000}})
    storage = CredentialTokenStorage(_credentials(), config, _URL)

    info = asyncio.run(storage.get_client_info())

    assert info.client_id == "pre"
    assert storage.redirect_port() == 4000


def test_connect_never_opens_a_browser_and_asks_for_login(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    manager = _manager(tmp_path, fake, _credentials())

    async def scenario():
        async with fake.mcp.session_manager.run():
            with patch("webbrowser.open") as browser:
                await manager.start(set())
            return manager.status(), browser.called

    (row,), opened = asyncio.run(scenario())
    assert row.auth == "oauth: needs login"
    assert "/mcp login intel" in row.state
    assert not opened
    assert fake.registrations == 0


def test_login_shows_the_origin_stores_tokens_and_later_connects_quietly(
    tmp_path: Path,
) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    manager = _manager(tmp_path, fake, credentials)
    open_browser, opened = _browser()
    origins: List[str] = []

    async def scenario():
        async with fake.mcp.session_manager.run():
            await manager.start(set())
            error = await manager.connect(
                "intel", login=_login(open_browser, origins=origins)
            )
            (row,) = manager.status()
            (tool,) = manager.tools()
            result = await tool.execute({"cve": "CVE-2026-0002"})
            await manager.close()
            return error, row, result

    error, row, result = asyncio.run(scenario())
    assert error is None
    assert origins == [_BASE]
    assert len(opened) == 1
    assert row.auth == "oauth: logged in"
    assert result.output == "CVE-2026-0002: critical"

    second = _FakeOAuthServer()
    second.issued = list(fake.issued)
    again = _manager(tmp_path, second, credentials)

    async def reconnect():
        async with second.mcp.session_manager.run():
            await again.start(set())
            (row,) = again.status()
            await again.close()
            return row

    with patch("webbrowser.open") as browser:
        row = asyncio.run(reconnect())
    assert row.state == "connected" and row.auth == "oauth: logged in"
    assert not browser.called
    assert second.registrations == 0


def test_declining_the_authorization_server_cancels_the_login(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    manager = _manager(tmp_path, fake, _credentials())
    open_browser, opened = _browser()

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            return await manager.connect(
                "intel", login=_login(open_browser, confirm=False)
            )

    error = asyncio.run(scenario())
    assert "cancelled" in error
    assert not opened


def test_logout_forgets_the_login(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    manager = _manager(tmp_path, fake, credentials)
    open_browser, _ = _browser()

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            await manager.connect("intel", login=_login(open_browser))
            await manager.logout("intel")
            return manager.status(), manager.tools()

    (row,), tools = asyncio.run(scenario())
    assert tools == []
    storage = CredentialTokenStorage(credentials, _server(), _URL)
    assert not storage.stored().logged_in


def test_pasted_callback_must_carry_the_matching_state() -> None:
    async def paste() -> Optional[str]:
        return "http://127.0.0.1/callback?code=c&state=forged"

    async def confirm(origin: str) -> bool:
        return True

    login = LoopbackAuthorization(
        free_loopback_port(),
        confirm_origin=confirm,
        paste_callback=paste,
        notify=lambda text: None,
        open_browser=lambda url: None,
    )

    async def scenario():
        await login.redirect_handler("https://as.example/authorize?state=real")
        with patch(
            "kerkov.providers.codex.oauth._wait_for_callback",
            return_value={"code": None, "error": "timeout_waiting_for_callback"},
        ):
            await login.callback_handler()

    with pytest.raises(LoginCancelled, match="state"):
        asyncio.run(scenario())


def test_an_expired_stored_token_is_refreshed_without_a_login(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    storage = CredentialTokenStorage(credentials, _server(), _URL)

    async def seed():
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=["http://127.0.0.1:4567/callback"],
                token_endpoint_auth_method="none",
            )
        )
        await storage.set_tokens(
            OAuthToken(access_token="stale", expires_in=-10, refresh_token="refresh-1")
        )

    asyncio.run(seed())
    manager = _manager(tmp_path, fake, credentials)

    async def scenario():
        async with fake.mcp.session_manager.run():
            await manager.start(set())
            (row,) = manager.status()
            await manager.close()
            return row

    with patch("webbrowser.open") as browser:
        row = asyncio.run(scenario())
    assert row.state == "connected"
    assert fake.refreshes == 1
    assert not browser.called


def test_a_login_revoked_mid_session_tells_the_model_to_ask_for_login(
    tmp_path: Path,
) -> None:
    fake = _FakeOAuthServer()
    manager = _manager(tmp_path, fake, _credentials())
    open_browser, _ = _browser()

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            await manager.connect("intel", login=_login(open_browser))
            (tool,) = manager.tools()
            fake.issued.clear()
            with patch("webbrowser.open") as browser:
                result = await tool.execute({"cve": "CVE-2026-0003"})
            (row,) = manager.status()
            await manager.close()
            return result, row, browser.called

    result, row, opened = asyncio.run(scenario())
    assert not result.success
    assert "/mcp login intel" in result.error
    assert row.auth == "oauth: needs login"
    assert not opened
