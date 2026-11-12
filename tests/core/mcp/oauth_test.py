"""OAuth for HTTP MCP servers: storage, the loopback login and connect-time rules."""

import asyncio
import json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.config import parse_server
from kerkov.core.mcp.manager import PLAINTEXT_OAUTH, McpManager
from kerkov.core.mcp.oauth import (
    CredentialTokenStorage,
    LoginCancelled,
    LoopbackAuthorization,
    forget_login,
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


_AS_PATHS = ("/register", "/token")
_ISSUER = "http://127.0.0.1:9100"


class _FakeOAuthServer:
    """An MCP resource server plus a minimal authorization server, over ASGI.

    `issuer` may name a different origin from the resource server's, which is what
    distinguishes a credential sent to the issuer from one sent to the resource:
    anything addressed to the issuer's endpoints but delivered to the resource's
    host lands in `misdirected`.
    """

    def __init__(self, issuer: str = _BASE, *, advertise_iss: bool = False) -> None:
        self.issued: List[str] = []
        self.registrations = 0
        self.refreshes = 0
        self.issuer = issuer
        self.advertise_iss = advertise_iss
        self.misdirected: List[Tuple[str, str]] = []
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
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if path in _AS_PATHS or path.startswith(
            "/.well-known/oauth-authorization-server"
        ):
            if headers.get("host", "") != urllib.parse.urlsplit(self.issuer).netloc:
                self.misdirected.append((path, (await _body(receive)).decode()))
                await _json(send, {"error": "not_found"}, status=404)
                return
            await self._authorization_server(path, receive, send)
            return
        if path.startswith("/.well-known/oauth-protected-resource"):
            await _json(
                send,
                {"resource": _URL, "authorization_servers": [self.issuer]},
            )
            return
        await self._resource(scope, receive, send, headers)

    async def _authorization_server(self, path: str, receive, send) -> None:
        if path.startswith("/.well-known/oauth-authorization-server"):
            await _json(
                send,
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token",
                    "registration_endpoint": f"{self.issuer}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "authorization_response_iss_parameter_supported": (
                        self.advertise_iss
                    ),
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
        else:
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

    async def _resource(self, scope, receive, send, headers) -> None:
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


def _manager(
    tmp_path: Path,
    fake: _FakeOAuthServer,
    credentials: CredentialManager,
    url: str = _URL,
):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"intel": {"type": "http", "url": url}}}),
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


def _browser(code: str = "the-code", iss: Optional[str] = None):
    """Stands in for the operator's browser: follows the redirect back to Kerkov."""
    opened: List[str] = []

    def open_browser(authorization_url: str) -> None:
        opened.append(authorization_url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        callback = f"{query['redirect_uri'][0]}?code={code}&state={query['state'][0]}"
        if iss is not None:
            callback += f"&iss={urllib.parse.quote(iss, safe='')}"

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

    assert forget_login(credentials, "intel") == []
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
            result = await manager.logout("intel")
            return manager.status(), manager.tools(), result

    (row,), tools, result = asyncio.run(scenario())
    assert tools == []
    assert result.applies and result.warnings == ()
    storage = CredentialTokenStorage(credentials, _server(), _URL)
    assert not storage.stored().logged_in
    assert storage.auth_metadata() is None


def test_logout_clears_a_login_whose_url_placeholder_is_missing(
    tmp_path: Path,
) -> None:
    credentials = _credentials()
    storage = CredentialTokenStorage(credentials, _server(), _URL)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", expires_in=3600)))
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"intel": {"type": "http", "url": "${GONE}/mcp"}}}),
        encoding="utf-8",
    )
    manager = McpManager(path, credentials=credentials, environment={})
    manager.load()

    result = asyncio.run(manager.logout("intel"))

    assert result.applies and result.warnings == ()
    assert not storage.stored().logged_in


def test_logout_reports_credentials_it_could_not_remove(tmp_path: Path) -> None:
    class _StuckStore(MemorySecretStore):
        def delete(self, key: str) -> None:
            raise OSError("the keyring is locked")

    credentials = CredentialManager(environment={}, persistent_store=_StuckStore())
    storage = CredentialTokenStorage(credentials, _server(), _URL)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", expires_in=3600)))
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"intel": {"type": "http", "url": _URL}}}),
        encoding="utf-8",
    )
    manager = McpManager(path, credentials=credentials, environment={})
    manager.load()

    result = asyncio.run(manager.logout("intel"))

    assert result.applies
    assert result.warnings and "could not be confirmed as removed" in result.warnings[0]
    # The warning is not cosmetic: the token is still readable.
    assert storage.stored().logged_in


def _paste_login(pasted: str) -> LoopbackAuthorization:
    async def paste() -> Optional[str]:
        return pasted

    async def confirm(origin: str) -> bool:
        return True

    return LoopbackAuthorization(
        free_loopback_port(),
        confirm_origin=confirm,
        paste_callback=paste,
        notify=lambda text: None,
        open_browser=lambda url: None,
    )


def _paste_result(login: LoopbackAuthorization):
    async def scenario():
        await login.redirect_handler("https://as.example/authorize?state=real")
        with patch(
            "kerkov.providers.codex.oauth._wait_for_callback",
            return_value={"code": None, "error": "timeout_waiting_for_callback"},
        ):
            return await login.callback_handler()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "pasted",
    [
        "http://127.0.0.1/callback?code=c&state=forged",
        # No state at all: the expected one must not be substituted for it.
        "http://127.0.0.1/callback?code=c",
    ],
)
def test_pasted_callback_must_carry_the_matching_state(pasted: str) -> None:
    with pytest.raises(LoginCancelled, match="state"):
        _paste_result(_paste_login(pasted))


def test_a_pasted_callback_forwards_its_issuer() -> None:
    result = _paste_result(
        _paste_login("http://127.0.0.1/callback?code=c&state=real&iss=https://as.test")
    )

    assert (result.code, result.state, result.iss) == ("c", "real", "https://as.test")


def test_a_bare_pasted_code_is_still_accepted() -> None:
    result = _paste_result(_paste_login("  the-code  "))

    assert (result.code, result.state, result.iss) == ("the-code", "real", None)


def test_an_empty_paste_cancels_the_login() -> None:
    with pytest.raises(LoginCancelled, match="no authorization code"):
        _paste_result(_paste_login(""))


def _seed_login(
    credentials: CredentialManager,
    *,
    issuer: Optional[str] = _BASE,
    client: bool = True,
) -> CredentialTokenStorage:
    """A stored, expired login as a restart would find it."""
    storage = CredentialTokenStorage(credentials, _server(), _URL)

    async def seed():
        if client:
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="client-1",
                    redirect_uris=["http://127.0.0.1:4567/callback"],
                    token_endpoint_auth_method="none",
                    issuer=issuer,
                )
            )
        await storage.set_tokens(
            OAuthToken(access_token="stale", expires_in=-10, refresh_token="refresh-1")
        )
        if issuer is not None:
            storage.set_auth_metadata(
                {
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "registration_endpoint": f"{issuer}/register",
                    "response_types_supported": ["code"],
                }
            )

    asyncio.run(seed())
    return storage


def _start(manager: McpManager, fake: _FakeOAuthServer):
    async def scenario():
        async with fake.mcp.session_manager.run():
            await manager.start(set())
            (row,) = manager.status()
            await manager.close()
            return row

    with patch("webbrowser.open") as browser:
        row = asyncio.run(scenario())
    assert not browser.called
    return row


def test_an_expired_stored_token_is_refreshed_without_a_login(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    _seed_login(credentials)

    row = _start(_manager(tmp_path, fake, credentials), fake)

    assert row.state == "connected"
    assert fake.refreshes == 1


def test_a_refresh_goes_to_the_recorded_issuer_not_the_resource(
    tmp_path: Path,
) -> None:
    fake = _FakeOAuthServer(issuer=_ISSUER)
    credentials = _credentials()
    _seed_login(credentials, issuer=_ISSUER)

    row = _start(_manager(tmp_path, fake, credentials), fake)

    assert row.state == "connected"
    assert fake.refreshes == 1
    assert fake.misdirected == []


def test_a_login_stored_without_its_issuer_is_never_refreshed(tmp_path: Path) -> None:
    fake = _FakeOAuthServer(issuer=_ISSUER)
    credentials = _credentials()
    _seed_login(credentials, issuer=None)

    row = _start(_manager(tmp_path, fake, credentials), fake)

    assert row.auth == "oauth: needs login"
    assert fake.refreshes == 0
    assert fake.misdirected == []


def test_a_login_whose_issuer_changed_is_never_refreshed(tmp_path: Path) -> None:
    fake = _FakeOAuthServer(issuer=_ISSUER)
    credentials = _credentials()
    storage = _seed_login(credentials, issuer=_ISSUER)
    # The stored metadata now names a server the client was never registered with.
    storage.set_auth_metadata(
        {
            "issuer": "http://127.0.0.1:9999",
            "authorization_endpoint": "http://127.0.0.1:9999/authorize",
            "token_endpoint": "http://127.0.0.1:9999/token",
            "response_types_supported": ["code"],
        }
    )

    row = _start(_manager(tmp_path, fake, credentials), fake)

    assert row.auth == "oauth: needs login"
    assert fake.refreshes == 0
    assert fake.misdirected == []


def test_a_login_records_the_issuer_it_was_granted_by(tmp_path: Path) -> None:
    fake = _FakeOAuthServer(issuer=_ISSUER)
    credentials = _credentials()
    manager = _manager(tmp_path, fake, credentials)
    open_browser, _ = _browser()

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            error = await manager.connect("intel", login=_login(open_browser))
            await manager.close()
            return error

    assert asyncio.run(scenario()) is None
    record = CredentialTokenStorage(credentials, _server(), _URL).auth_metadata()
    assert record["metadata"]["issuer"] == _ISSUER
    assert fake.misdirected == []


def test_a_connect_never_registers_a_client(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    # A stored token with no registration beside it: the SDK would register a new
    # client before it ever asked the redirect handler.
    _seed_login(credentials, issuer=None, client=False)

    row = _start(_manager(tmp_path, fake, credentials), fake)

    assert row.auth == "oauth: needs login"
    assert fake.registrations == 0


def test_a_login_forwards_the_authorization_response_issuer(tmp_path: Path) -> None:
    fake = _FakeOAuthServer(advertise_iss=True)
    manager = _manager(tmp_path, fake, _credentials())
    open_browser, _ = _browser(iss=_BASE)

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            error = await manager.connect("intel", login=_login(open_browser))
            (row,) = manager.status()
            await manager.close()
            return error, row

    error, row = asyncio.run(scenario())
    assert error is None
    assert row.auth == "oauth: logged in"


def test_a_login_refuses_a_forged_authorization_response_issuer(
    tmp_path: Path,
) -> None:
    fake = _FakeOAuthServer(advertise_iss=True)
    manager = _manager(tmp_path, fake, _credentials())
    open_browser, _ = _browser(iss="http://127.0.0.1:9999")

    async def scenario():
        async with fake.mcp.session_manager.run():
            manager.load()
            error = await manager.connect("intel", login=_login(open_browser))
            await manager.close()
            return error

    assert "iss" in asyncio.run(scenario())


def test_oauth_is_refused_over_plain_http_to_a_remote_host(tmp_path: Path) -> None:
    fake = _FakeOAuthServer()
    credentials = _credentials()
    remote = "http://mcp.example.com/mcp"
    storage = CredentialTokenStorage(credentials, _server(), remote)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", expires_in=3600)))
    manager = _manager(tmp_path, fake, credentials, url=remote)

    row = _start(manager, fake)

    assert "https" in row.state
    assert row.auth == "oauth: refused"
    assert manager.login_problem("intel") == PLAINTEXT_OAUTH
    assert manager.oauth_problem("intel") is None


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
