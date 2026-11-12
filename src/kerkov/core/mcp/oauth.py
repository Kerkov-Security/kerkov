"""OAuth 2.1 for HTTP MCP servers: token storage and the interactive login.

Tokens, client registrations and the authorization server that issued them live in
the `CredentialManager` (OS keyring or the protected fallback file), bound to the
server URL they were issued for. A connect never starts a login: without an
interactive handler, an authorization request — or a dynamic client registration,
which the SDK sends first — marks the server as needing `/mcp login` and fails the
connection.
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
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    List,
    NoReturn,
    Optional,
    Tuple,
)

from kerkov.core.credentials import CredentialManager, CredentialSpec
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.mcp.config import McpServerConfig

logger = get_logger("McpOAuth")

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT_SECONDS = 300
LOGIN_TIMEOUT_SECONDS = CALLBACK_TIMEOUT_SECONDS + 60
CLIENT_NAME = "Kerkov"
# One record per part: a keyring entry has a size limit, and `metadata` names the
# issuer the other two were granted by.
STORED_PARTS = ("tokens", "client", "metadata")
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

    def auth_metadata(self) -> Optional[Dict[str, Any]]:
        """The record of the authorization server this login was granted by.

        None when nothing is recorded; a record whose `metadata` is None means the
        login was granted without authorization server metadata.
        """
        return self._read("metadata")

    def set_auth_metadata(self, metadata: Optional[Dict[str, Any]]) -> None:
        self._write("metadata", {"metadata": metadata})

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


def forget_login(credentials: CredentialManager, server_key: str) -> List[str]:
    """Delete every stored OAuth record for one server; returns what was left behind.

    Keyed on the server name alone: an unresolvable `${KEY}` in the URL must not
    leave a usable token stored.
    """
    warnings: List[str] = []
    for part in STORED_PARTS:
        try:
            warning = credentials.delete(_spec(server_key, part))
        except Exception as exc:
            warning = f"The stored OAuth {part} could not be removed: {exc}"
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


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

        from kerkov.providers.codex.oauth import _wait_for_callback

        result = await asyncio.to_thread(
            _wait_for_callback,
            host=CALLBACK_HOST,
            port=self.port,
            callback_path=CALLBACK_PATH,
            expected_state=self._state or "",
            timeout_seconds=CALLBACK_TIMEOUT_SECONDS,
            page_title="Kerkov MCP login",
        )
        code, issuer = result["code"], result.get("iss")
        if result["error"] not in (None, "bind_failed", "timeout_waiting_for_callback"):
            self._fail(f"authorization failed: {result['error']}")
        if not code:
            code, issuer = await self._from_paste()
        self._completed = True
        # `iss` is RFC 9207: the SDK checks it against the discovered issuer, and
        # rejects its absence when the server advertised the parameter.
        return AuthorizationCodeResult(code=code, state=self._state, iss=issuer)

    def before_registration(self) -> None:
        """Dynamic registration is part of a login, but only until it completes."""
        if not self._completed:
            return
        self.login_required = True
        raise LoginRequired("this server needs `/mcp login`")

    async def _from_paste(self) -> Tuple[str, Optional[str]]:
        """The code and issuer from a pasted callback, or a bare code typed by hand."""
        pasted = (await self._paste_callback() or "").strip()
        if "://" not in pasted:
            if not pasted:
                self._fail("no authorization code was given")
            # A bare code carries no state or issuer to check; the operator read
            # it off the authorization server's own page.
            return pasted, None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
        if query.get("state", [None])[0] != self._state:
            self._fail("the pasted callback's state did not match")
        code = query.get("code", [None])[0]
        if not code:
            self._fail("no authorization code was given")
        return code, query.get("iss", [None])[0]

    def _fail(self, message: str) -> NoReturn:
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

    def before_registration(self) -> NoReturn:
        self.login_required = True
        raise LoginRequired("this server needs `/mcp login` to register a client")


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((CALLBACK_HOST, 0))
        return int(probe.getsockname()[1])


def restore_auth_server(
    storage: CredentialTokenStorage, client_info: Any
) -> Tuple[Any, bool]:
    """The stored login's authorization server metadata, and whether it is bound.

    Unbound means no credential may be sent before rediscovery: with no metadata
    the SDK's token endpoint falls back to the *resource* server's own origin, so
    a refresh would hand the refresh token to whoever answers there.
    """
    from mcp.client.auth.utils import credentials_match_issuer
    from mcp.shared.auth import OAuthMetadata
    from pydantic import ValidationError

    record = storage.auth_metadata()
    if record is None:
        return None, False
    raw = record.get("metadata")
    if raw is None:
        # The login was granted with no authorization server metadata, so the
        # SDK's fallback endpoints on the resource's own origin are the issuer.
        return None, True
    try:
        metadata = OAuthMetadata.model_validate(raw)
    except ValidationError:
        return None, False
    if client_info is not None and not credentials_match_issuer(
        client_info, str(metadata.issuer), None
    ):
        return None, False
    return metadata, True


def _registers_a_client(request: Any) -> bool:
    """Dynamic client registration is the only JSON POST the SDK's flow sends."""
    if request.method != "POST":
        return False
    content_type = str(request.headers.get("content-type", ""))
    return content_type.startswith("application/json")


def build_oauth_provider(
    server: McpServerConfig,
    url: str,
    storage: CredentialTokenStorage,
    interactive: Optional[LoopbackAuthorization],
) -> tuple[Any, Any]:
    """An SDK `OAuthClientProvider` and its handlers, which flag `login_required`."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    handlers: Any = interactive or _NonInteractive()

    class _Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            metadata, bound = restore_auth_server(storage, self.context.client_info)
            if not bound:
                return
            self.context.oauth_metadata = metadata
            # The SDK only learns a token's lifetime when it is issued, so a
            # token loaded after a restart would be sent until it was refused,
            # and a refused token goes to a full login instead of a refresh.
            # Only safe with the issuer above restored: the refresh goes there.
            self.context.token_expiry_time = storage.expires_at()

        async def _handle_token_response(self, response: Any) -> None:
            await super()._handle_token_response(response)
            discovered = self.context.oauth_metadata
            storage.set_auth_metadata(
                discovered.model_dump(mode="json") if discovered else None
            )

        async def _auth_flow(self, request: Any) -> AsyncGenerator[Any, Any]:
            # The SDK registers a client before it calls the redirect handler, so
            # refusing there is too late to keep an ordinary connect from minting
            # a client at a server-named endpoint.
            inner = super()._auth_flow(request)
            try:
                outgoing = await inner.__anext__()
                while True:
                    if outgoing is not request and _registers_a_client(outgoing):
                        handlers.before_registration()
                    response = yield outgoing
                    outgoing = await inner.asend(response)
            except StopAsyncIteration:
                return
            finally:
                await inner.aclose()

    port = interactive.port if interactive else storage.redirect_port() or 0
    metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=server.oauth.scope,
    )
    provider = _Provider(
        server_url=url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=handlers.redirect_handler,
        callback_handler=handlers.callback_handler,
    )
    return provider, handlers
