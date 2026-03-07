import base64
import hashlib
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx

from src.core.config_manager import ConfigManager
from src.providers.codex_auth import (
    DEFAULT_CODEX_AUTHORIZE_URL,
    DEFAULT_CODEX_CLIENT_ID,
    DEFAULT_CODEX_REDIRECT_URI,
    DEFAULT_CODEX_SCOPE,
    DEFAULT_CODEX_TOKEN_URL,
    extract_account_id_from_jwt,
    find_env_path,
    upsert_env_values,
)


def run_codex_oauth_browser_login(
    config: ConfigManager,
    timeout_seconds: int = 300,
) -> Tuple[bool, str]:
    authorize_url = config.codex_oauth_authorize_url or DEFAULT_CODEX_AUTHORIZE_URL
    token_url = config.codex_oauth_token_url or DEFAULT_CODEX_TOKEN_URL
    client_id = config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID
    redirect_uri = config.codex_oauth_redirect_uri or DEFAULT_CODEX_REDIRECT_URI
    scope = config.codex_oauth_scope or DEFAULT_CODEX_SCOPE

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    if parsed_redirect.hostname is None or parsed_redirect.port is None or not parsed_redirect.path:
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
        host=parsed_redirect.hostname,
        port=parsed_redirect.port,
        callback_path=parsed_redirect.path,
        expected_state=state,
        timeout_seconds=timeout_seconds,
    )
    if callback_result["error"] == "bind_failed":
        print("Could not bind local callback server. Switching to manual paste mode.")
    elif callback_result["error"] and callback_result["error"] != "timeout_waiting_for_callback":
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

    token_payload: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if config.codex_oauth_client_secret:
        token_payload["client_secret"] = config.codex_oauth_client_secret

    try:
        with httpx.Client(timeout=config.max_timeout_seconds) as client:
            token_response = client.post(token_url, data=token_payload)
            token_response.raise_for_status()
            token_data = token_response.json()
    except Exception as ex:
        return False, f"Token exchange failed: {ex}"

    access_token = token_data.get("access_token")
    id_token = token_data.get("id_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        return False, "Token endpoint did not return access_token."
    if not refresh_token and not config.codex_oauth_refresh_token:
        return False, "Token endpoint did not return refresh_token and no existing refresh token is configured."

    env_path = find_env_path(Path.cwd())
    if env_path is None:
        return False, "Could not find .env file from current directory."

    updates = {
        "CODEX_ACCESS_TOKEN": access_token,
    }
    if refresh_token:
        updates["CODEX_OAUTH_REFRESH_TOKEN"] = refresh_token

    account_id = extract_account_id_from_jwt(access_token)
    if account_id:
        updates["CODEX_ACCOUNT_ID"] = account_id

    upsert_env_values(env_path, updates)

    expires_in = token_data.get("expires_in")
    expiry_msg = ""
    if expires_in:
        expires_at = int(time.time() + int(expires_in))
        upsert_env_values(env_path, {"CODEX_TOKEN_EXPIRES_AT": str(expires_at)})
        expiry_msg = f" Access token expires in ~{expires_in}s (unix {expires_at})."
    account_msg = f" accountId={account_id}." if account_id else ""

    return True, f"Codex OAuth login complete. Tokens saved to {env_path}.{account_msg}{expiry_msg}"


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
) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {"code": None, "error": None}

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                result["error"] = params.get("error", ["unknown_error"])[0]
                self._send_html("OAuth failed. You can close this tab.")
                return

            state = params.get("state", [""])[0]
            if state != expected_state:
                result["error"] = "state_mismatch"
                self._send_html("Invalid OAuth state. You can close this tab.")
                return

            result["code"] = params.get("code", [""])[0] or None
            self._send_html("OAuth complete. You can close this tab and return to the terminal.")

        def log_message(self, format, *args):  # noqa: A003
            return

        def _send_html(self, message: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                (
                    "<html><head><title>Codex OAuth</title></head>"
                    f"<body><h3>{message}</h3></body></html>"
                ).encode("utf-8")
            )

    try:
        server = HTTPServer((host, port), OAuthCallbackHandler)
    except OSError:
        result["error"] = "bind_failed"
        return result

    server.timeout = 1
    deadline = time.time() + timeout_seconds

    while time.time() < deadline and not result["code"] and not result["error"]:
        server.handle_request()

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
