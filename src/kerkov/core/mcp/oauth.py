"""OAuth 2.1 for HTTP MCP servers: token storage and the interactive login.

Tokens and client registrations live in the `CredentialManager` (OS keyring or the
protected fallback file), bound to the server URL they were issued for. A connect
never starts a login: without an interactive handler, an authorization request
marks the server as needing `/mcp login` and fails the connection.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from kerkov.core.credentials import CredentialManager, CredentialSpec
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.mcp.config import McpServerConfig

logger = get_logger("McpOAuth")

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT_SECONDS = 300
LOGIN_TIMEOUT_SECONDS = CALLBACK_TIMEOUT_SECONDS + 60
CLIENT_NAME = "Kerkov"
_KEY_CHARS = re.compile(r"[^A-Z0-9_]")

ConfirmOrigin = Callable[[str], Awaitable[bool]]
PasteCallback = Callable[[], Awaitable[Optional[str]]]


class LoginRequired(RuntimeError):
    """The server needs an interactive login, which only `/mcp login` may start."""


class LoginCancelled(RuntimeError):
    """The operator declined the authorization server or gave no code."""


@dataclass(frozen=True)
class StoredLogin:
    """What is stored for one server, without exposing any secret."""

    logged_in: bool
    expired: bool
    refreshable: bool


def _spec(server_key: str, part: str) -> CredentialSpec:
    suffix = _KEY_CHARS.sub("_", server_key.upper())
    return CredentialSpec(
        key=f"mcp.oauth.{server_key}.{part}",
        env_var=f"KERKOV_MCP_OAUTH_{suffix}_{part.upper()}",
        label=f"MCP OAuth {part} for {server_key}",
    )


class CredentialTokenStorage:
    """The SDK's `TokenStorage`, backed by the credential store.

    Each record carries the URL it was issued for, so editing a server's URL in
    `mcp.json` can never send an old token to a different server.
    """

    def __init__(
        self, credentials: CredentialManager, server: McpServerConfig, server_url: str
    ) -> None:
        self._credentials = credentials
        self._server = server
        self._server_key = server.key
        self._server_url = server_url

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        record = self._read("tokens")
        return OAuthToken.model_validate(record["tokens"]) if record else None

    async def set_tokens(self, tokens: Any) -> None:
        expires_at = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        self._write(
            "tokens",
            {"tokens": tokens.model_dump(mode="json"), "expires_at": expires_at},
        )

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        record = self._read("client")
        if record:
            return OAuthClientInformationFull.model_validate(record["client"])
        settings = self._server.oauth
        if settings.client_id and settings.callback_port:
            # A pre-registered public client: no dynamic registration needed.
            return OAuthClientInformationFull(
                client_id=settings.client_id,
                redirect_uris=[
                    f"http://{CALLBACK_HOST}:{settings.callback_port}{CALLBACK_PATH}"
                ],
                token_endpoint_auth_method="none",
            )
        return None

    async def set_client_info(self, client_info: Any) -> None:
        self._write("client", {"client": client_info.model_dump(mode="json")})

    def expires_at(self) -> Optional[float]:
        record = self._read("tokens")
        return record.get("expires_at") if record else None

    def redirect_port(self) -> Optional[int]:
        """The loopback port the client is registered with, if it has one yet."""
        record = self._read("client")
        uris = (record or {}).get("client", {}).get("redirect_uris") or []
        if uris:
            return urllib.parse.urlsplit(str(uris[0])).port
        return self._server.oauth.callback_port

    def stored(self) -> StoredLogin:
        record = self._read("tokens")
        if not record:
            return StoredLogin(logged_in=False, expired=False, refreshable=False)
        expires_at = record.get("expires_at")
        refreshable = bool(record["tokens"].get("refresh_token"))
        expired = expires_at is not None and time.time() > expires_at
        return StoredLogin(logged_in=True, expired=expired, refreshable=refreshable)

    def clear(self) -> None:
        for part in ("tokens", "client"):
            try:
                self._credentials.delete(_spec(self._server_key, part))
            except Exception as exc:
                logger.warning("Could not remove MCP OAuth %s: %s", part, exc)

    def _read(self, part: str) -> Optional[Dict[str, Any]]:
        raw = self._credentials.get(_spec(self._server_key, part))
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("url") != self._server_url:
            return None
        return record

    def _write(self, part: str, record: Dict[str, Any]) -> None:
        payload = json.dumps({"url": self._server_url, **record})
        result = self._credentials.save(_spec(self._server_key, part), payload)
        if result.warning:
            logger.warning(
                "MCP OAuth %s for %s: %s", part, self._server_key, result.warning
            )


class LoopbackAuthorization:
    """Redirect and callback handlers for one interactive login.

    The authorization server's origin is shown before anything opens: its URL
    comes from server-controlled metadata, which in an engagement may be hostile.
    """

    def __init__(
        self,
        port: int,
        *,
        confirm_origin: ConfirmOrigin,
        paste_callback: PasteCallback,
        notify: Callable[[str], None],
        open_browser: Callable[[str], Any] = webbrowser.open,
    ) -> None:
        self.port = port
        self._confirm_origin = confirm_origin
        self._paste_callback = paste_callback
        self._notify = notify
        self._open_browser = open_browser
        self._state: Optional[str] = None
        # The SDK may wrap what these handlers raise; the manager reports this.
        self.failure: Optional[str] = None
        self.login_required = False
        self._completed = False

    @property
    def redirect_uri(self) -> str:
        return f"http://{CALLBACK_HOST}:{self.port}{CALLBACK_PATH}"

    async def redirect_handler(self, authorization_url: str) -> None:
        if self._completed:
            # The connection outlives `/mcp login`; a later refusal must not
            # prompt in the middle of a tool call.
            self.login_required = True
            raise LoginRequired("this server needs `/mcp login`")
        parts = urllib.parse.urlsplit(authorization_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if not await self._confirm_origin(origin):
            self._fail(f"login to {origin} cancelled")
        self._state = urllib.parse.parse_qs(parts.query).get("state", [None])[0]
        self._notify(f"If the browser does not open, visit:\n{authorization_url}")
        self._open_browser(authorization_url)

    async def callback_handler(self) -> Any:
        from mcp.client.auth import AuthorizationCodeResult

        from kerkov.providers.codex.oauth import (
            _parse_manual_code_or_url,
            _wait_for_callback,
        )

        result = await asyncio.to_thread(
            _wait_for_callback,
            host=CALLBACK_HOST,
            port=self.port,
            callback_path=CALLBACK_PATH,
            expected_state=self._state or "",
            timeout_seconds=CALLBACK_TIMEOUT_SECONDS,
            page_title="Kerkov MCP login",
        )
        code = result["code"]
        if result["error"] not in (None, "bind_failed", "timeout_waiting_for_callback"):
            self._fail(f"authorization failed: {result['error']}")
        if not code:
            pasted = await self._paste_callback()
            code, state = _parse_manual_code_or_url(pasted or "")
            if state and state != self._state:
                self._fail("the pasted callback's state did not match")
            if not code:
                self._fail("no authorization code was given")
        self._completed = True
        return AuthorizationCodeResult(code=code, state=self._state)

    def _fail(self, message: str) -> None:
        self.failure = message
        raise LoginCancelled(message)


class _NonInteractive:
    """Handlers for ordinary connects: an authorization request means login."""

    def __init__(self) -> None:
        self.login_required = False

    async def redirect_handler(self, authorization_url: str) -> None:
        self.login_required = True
        raise LoginRequired("this server needs `/mcp login`")

    async def callback_handler(self) -> Any:
        raise LoginRequired("this server needs `/mcp login`")


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((CALLBACK_HOST, 0))
        return int(probe.getsockname()[1])


def build_oauth_provider(
    server: McpServerConfig,
    url: str,
    storage: CredentialTokenStorage,
    interactive: Optional[LoopbackAuthorization],
) -> tuple[Any, Any]:
    """An SDK `OAuthClientProvider` and its handlers, which flag `login_required`."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    class _Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            # The SDK only learns a token's lifetime when it is issued, so a
            # token loaded after a restart would be sent until it was refused,
            # and a refused token goes to a full login instead of a refresh.
            self.context.token_expiry_time = storage.expires_at()

    port = interactive.port if interactive else storage.redirect_port() or 0
    metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=server.oauth.scope,
    )
    handlers = interactive or _NonInteractive()
    provider = _Provider(
        server_url=url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=handlers.redirect_handler,
        callback_handler=handlers.callback_handler,
    )
    return provider, handlers
