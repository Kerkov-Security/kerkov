import base64
import hashlib
import html
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
    extract_account_id_from_jwt,
    persist_codex_state,
)


def run_codex_oauth_browser_login(
    config: ConfigManager,
    timeout_seconds: int = 300,
) -> Tuple[bool, str]:
    authorize_url = config.codex_oauth_authorize_url or DEFAULT_CODEX_AUTHORIZE_URL
    client_id = config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID
    redirect_uri = config.codex_oauth_redirect_uri or DEFAULT_CODEX_REDIRECT_URI
    scope = config.codex_oauth_scope or DEFAULT_CODEX_SCOPE

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    if (
        parsed_redirect.hostname is None
        or parsed_redirect.port is None
        or not parsed_redirect.path
    ):
        return (
            False,
            "CODEX_OAUTH_REDIRECT_URI must include host, port, and path (e.g. http://localhost:1455/auth/callback).",
        )

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(24)

    auth_url = _build_authorize_url(
        authorize_url=authorize_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
        code_challenge=code_challenge,
    )

    print("\nOpening browser for Codex OAuth login...")
    print(f"If the browser does not open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url, new=2)

    callback_result = _wait_for_callback(
        host=config.codex_oauth_callback_bind_host or parsed_redirect.hostname,
        port=parsed_redirect.port,
        callback_path=parsed_redirect.path,
        expected_state=state,
        timeout_seconds=timeout_seconds,
    )
    if callback_result["error"] == "bind_failed":
        print("Could not bind local callback server. Switching to manual paste mode.")
    elif (
        callback_result["error"]
        and callback_result["error"] != "timeout_waiting_for_callback"
    ):
        return False, f"OAuth callback failed: {callback_result['error']}"

    auth_code = callback_result["code"]
    if not auth_code:
        print(
            "If browser callback did not auto-complete, paste full redirected URL "
            "(or just the code):"
        )
        pasted = input("> ").strip()
        parsed_code, parsed_state = _parse_manual_code_or_url(pasted)
        if not parsed_code:
            return False, "No authorization code found in pasted input."
        if parsed_state and parsed_state != state:
            return False, "Pasted callback state did not match expected state."
        auth_code = parsed_code

    if not auth_code:
        return False, "OAuth callback did not contain an authorization code."

    try:
        updates = exchange_codex_oauth_code(config, auth_code, code_verifier)
        state_path = persist_codex_state(Path(config.codex_state_path), updates)
    except Exception as ex:
        return False, f"Token exchange failed: {ex}"
    return True, f"Codex OAuth login complete. Tokens saved to {state_path}."


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
                result["error"] = "authorization_denied"
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
