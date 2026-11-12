import base64
import hashlib
import html
import ipaddress
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.auth import (
    DEFAULT_CODEX_AUTHORIZE_URL,
    DEFAULT_CODEX_CLIENT_ID,
    DEFAULT_CODEX_REDIRECT_URI,
    DEFAULT_CODEX_SCOPE,
    DEFAULT_CODEX_TOKEN_URL,
    CodexAuthConfig,
    CodexTokenManager,
    extract_account_id_from_jwt,
    normalize_codex_state_path,
    validate_codex_login,
)


class CodexLoginFlow:
    """Share PKCE, callback validation and credential installation across front ends."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config.model_copy(update={"codex_oauth_refresh_token": None})
        self.state_path = normalize_codex_state_path(Path(config.codex_state_path))
        try:
            redirect = urllib.parse.urlsplit(
                config.codex_oauth_redirect_uri or DEFAULT_CODEX_REDIRECT_URI
            )
            valid_redirect = (
                redirect.scheme == "http"
                and redirect.hostname in {"localhost", "127.0.0.1"}
                and redirect.port is not None
                and redirect.port > 0
                and bool(redirect.path)
                and redirect.username is None
                and redirect.password is None
                and not redirect.query
                and not redirect.fragment
            )
        except ValueError:
            valid_redirect = False
        if not valid_redirect:
            raise ValueError(
                "Codex sign-in requires a local HTTP callback URL with a port and path."
            )
        authorize_url = config.codex_oauth_authorize_url or DEFAULT_CODEX_AUTHORIZE_URL
        authorize = urllib.parse.urlsplit(authorize_url)
        if (
            authorize.scheme != "https"
            or not authorize.hostname
            or authorize.username
            or authorize.password
        ):
            raise ValueError("Codex sign-in requires an HTTPS authorization URL.")
        self.bind_host = config.codex_oauth_callback_bind_host or redirect.hostname
        if self.bind_host != "localhost":
            try:
                address = ipaddress.IPv4Address(self.bind_host)
            except ipaddress.AddressValueError as exc:
                raise ValueError(
                    "Codex callback bind host must be loopback or 0.0.0.0."
                ) from exc
            if not address.is_loopback and not address.is_unspecified:
                raise ValueError(
                    "Codex callback bind host must be loopback or 0.0.0.0."
                )
        self._redirect = redirect
        self._verifier = _generate_code_verifier()
        self._state = secrets.token_urlsafe(24)
        self.authorize_url = _build_authorize_url(
            authorize_url,
            config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID,
            redirect.geturl(),
            config.codex_oauth_scope or DEFAULT_CODEX_SCOPE,
            self._state,
            _generate_code_challenge(self._verifier),
        )

    def wait_for_callback(
        self,
        timeout_seconds: int = 300,
        *,
        cancelled: Optional[threading.Event] = None,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Optional[str]]:
        return _wait_for_callback(
            host=self.bind_host,
            port=self._redirect.port,
            callback_path=self._redirect.path,
            expected_state=self._state,
            timeout_seconds=timeout_seconds,
            cancelled=cancelled,
            on_ready=on_ready,
        )

    def manual_code(self, raw: str) -> str:
        code, state = _parse_manual_code_or_url(raw)
        if not code:
            raise ValueError("No authorization code found in pasted input.")
        if state and not secrets.compare_digest(state.encode(), self._state.encode()):
            raise ValueError("Pasted callback state did not match expected state.")
        return code

    def exchange(self, code: str) -> Dict[str, str]:
        return validate_codex_login(
            exchange_codex_oauth_code(self.config, code, self._verifier)
        )

    def install(
        self,
        updates: Dict[str, str],
        existing: Optional[CodexTokenManager] = None,
    ) -> CodexTokenManager:
        manager = (
            existing
            if existing is not None and existing.state_path == self.state_path
            else CodexTokenManager(
                CodexAuthConfig(
                    self.state_path,
                    self.config.codex_oauth_token_url or "",
                    self.config.codex_oauth_client_id,
                    self.config.codex_oauth_client_secret,
                    self.config.max_timeout_seconds,
                )
            )
        )
        manager.install_login(updates)
        return manager


def run_codex_oauth_browser_login(
    config: ConfigManager,
    timeout_seconds: int = 300,
) -> Tuple[bool, str]:
    try:
        flow = CodexLoginFlow(config)
    except ValueError as exc:
        return False, str(exc)
    print("\nOpening browser for Codex OAuth login...")
    print(f"If the browser does not open, visit:\n{flow.authorize_url}\n")
    webbrowser.open(flow.authorize_url, new=2)
    callback = flow.wait_for_callback(timeout_seconds)
    error = callback["error"]
    if error == "bind_failed":
        print("Could not bind local callback server. Switching to manual paste mode.")
    elif error and error not in {"timeout_waiting_for_callback", "missing_code"}:
        return False, f"OAuth callback failed: {error}"
    code = callback["code"]
    if not code:
        print(
            "If browser callback did not auto-complete, paste full redirected URL (or just the code):"
        )
        try:
            code = flow.manual_code(input("> ").strip())
        except ValueError as exc:
            return False, str(exc)
    try:
        flow.install(flow.exchange(code))
    except Exception as exc:
        return False, f"Token exchange failed: {exc}"
    return True, f"Codex OAuth login complete. Tokens saved to {flow.state_path}."


def exchange_codex_oauth_code(
    config: ConfigManager, auth_code: str, code_verifier: str
) -> Dict[str, str]:
    """Exchange a PKCE code without persisting credentials."""
    token_payload: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID,
        "redirect_uri": config.codex_oauth_redirect_uri or DEFAULT_CODEX_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    if config.codex_oauth_client_secret:
        token_payload["client_secret"] = config.codex_oauth_client_secret

    with httpx.Client(timeout=config.max_timeout_seconds) as client:
        token_response = client.post(
            config.codex_oauth_token_url or DEFAULT_CODEX_TOKEN_URL, data=token_payload
        )
        token_response.raise_for_status()
        token_data = token_response.json()

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Token endpoint did not return access_token.")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = config.codex_oauth_refresh_token
    if not refresh_token:
        raise ValueError("Token endpoint did not return refresh_token.")

    updates = {
        "CODEX_ACCESS_TOKEN": access_token,
        "CODEX_TOKEN_EXPIRES_AT": "",
    }
    if isinstance(refresh_token, str) and refresh_token:
        updates["CODEX_OAUTH_REFRESH_TOKEN"] = refresh_token

    account_id = extract_account_id_from_jwt(access_token)
    if account_id:
        updates["CODEX_ACCOUNT_ID"] = account_id

    expires_in = token_data.get("expires_in")
    if expires_in:
        updates["CODEX_TOKEN_EXPIRES_AT"] = str(int(time.time() + int(expires_in)))
    return updates


def _build_authorize_url(
    authorize_url: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "pi",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{authorize_url}?{urllib.parse.urlencode(params)}"


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _generate_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def _wait_for_callback(
    host: str,
    port: int,
    callback_path: str,
    expected_state: str,
    timeout_seconds: int,
    *,
    cancelled: Optional[threading.Event] = None,
    on_ready: Optional[Callable[[], None]] = None,
) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {"code": None, "error": None}

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(1)
            super().setup()

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            params = urllib.parse.parse_qs(parsed.query)
            state = params.get("state", [""])[0]
            if not secrets.compare_digest(state.encode(), expected_state.encode()):
                self._send_html("Invalid OAuth state. Return to Kerkov and retry.")
                return
            if "error" in params:
                error = params["error"][0]
                result["error"] = (
                    error
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error)
                    else "authorization_failed"
                )
                self._send_html("OAuth failed. You can close this tab.")
                return

            result["code"] = params.get("code", [""])[0] or None
            if not result["code"]:
                result["error"] = "missing_code"
                self._send_html("OAuth failed. Return to Kerkov and retry.")
                return
            self._send_html(
                "OAuth complete. You can close this tab and return to Kerkov."
            )

        def log_message(self, format, *args):  # noqa: A003
            return

        def _send_html(self, message: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            safe_message = html.escape(message, quote=True)
            self.wfile.write(
                (
                    "<html><head><title>Codex OAuth</title></head>"
                    f"<body><h3>{safe_message}</h3></body></html>"
                ).encode("utf-8")
            )

    try:
        server = HTTPServer((host, port), OAuthCallbackHandler)
    except OSError:
        result["error"] = "bind_failed"
        return result

    server.timeout = 0.25
    deadline = time.monotonic() + timeout_seconds
    try:
        if on_ready is not None:
            on_ready()
        while (
            time.monotonic() < deadline and not result["code"] and not result["error"]
        ):
            if cancelled is not None and cancelled.is_set():
                result["error"] = "cancelled"
                break
            server.handle_request()
    finally:
        server.server_close()

    if not result["code"] and not result["error"]:
        result["error"] = "timeout_waiting_for_callback"
    return result


def _parse_manual_code_or_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    raw = raw.strip()
    if not raw:
        return None, None

    if "://" not in raw:
        return raw, None

    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    return code, state
