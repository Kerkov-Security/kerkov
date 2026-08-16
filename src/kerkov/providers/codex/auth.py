import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import httpx

from kerkov.core.json_state import load_json_file, write_json_file

DEFAULT_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_CODEX_SCOPE = "openid profile email offline_access"
DEFAULT_CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"


@dataclass(frozen=True)
class CodexAuthConfig:
    state_path: Path
    token_url: str
    client_id: Optional[str]
    client_secret: Optional[str]
    timeout_seconds: int


def extract_account_id_from_jwt(jwt_token: str) -> Optional[str]:
    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        claims = json.loads(decoded)
        auth_claim = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            account_id = auth_claim.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
        return None
    except Exception:
        return None


def load_codex_state(state_path: Path) -> dict[str, str]:
    state = load_json_file(state_path)
    return {str(key): str(value) for key, value in state.items() if value is not None}


def codex_auth_state_is_configured(state_path: Path) -> bool:
    """Return whether the local Codex state contains usable authentication."""

    state = load_codex_state(state_path)
    return bool(
        state.get("CODEX_ACCESS_TOKEN") or state.get("CODEX_OAUTH_REFRESH_TOKEN")
    )


def clear_codex_auth_state(state_path: Path) -> bool:
    """Remove the local Codex OAuth state file without following symlinks."""

    if not state_path.exists() and not state_path.is_symlink():
        return False
    state_path.unlink()
    return True


def persist_codex_state(
    state_path: Path,
    updates: Dict[str, str],
) -> Path:
    existing = load_json_file(state_path)
    merged = {**existing, **updates}
    write_json_file(state_path, merged)
    return state_path


class CodexTokenManager:
    """Handles Codex access token lifecycle (read, refresh, persist)."""

    def __init__(
        self,
        auth_config: CodexAuthConfig,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        account_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ):
        self._auth_config = auth_config
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.account_id = account_id
        if not self.account_id and self.access_token:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        self.expires_at = self._parse_unix_ts(expires_at)

    def ensure_valid_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self._refresh_access_token()
        elif not self.access_token and self.refresh_token:
            self._refresh_access_token()
        elif self._is_expiring_soon() and self.refresh_token:
            self._refresh_access_token()

        if not self.access_token:
            raise ValueError(
                "Missing Codex access token. Select Codex with /provider to sign in."
            )

        if not self.account_id:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        if not self.account_id:
            raise ValueError(
                "Missing ChatGPT account id in token. Select Codex with /provider "
                "to sign in again."
            )

        return self.access_token

    def validate_available_auth(self) -> bool:
        try:
            self.ensure_valid_token(force_refresh=False)
            return True
        except Exception:
            return False

    def _is_expiring_soon(self, skew_seconds: int = 30) -> bool:
        if self.expires_at is None:
            return False
        return (self.expires_at - int(time.time())) <= skew_seconds

    def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise ValueError(
                "Missing CODEX_OAUTH_REFRESH_TOKEN. Select Codex with /provider "
                "to sign in again."
            )

        token_url = self._auth_config.token_url or DEFAULT_CODEX_TOKEN_URL
        client_id = self._auth_config.client_id or DEFAULT_CODEX_CLIENT_ID

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": client_id,
        }
        if self._auth_config.client_secret:
            payload["client_secret"] = self._auth_config.client_secret

        with httpx.Client(timeout=self._auth_config.timeout_seconds) as client:
            response = client.post(token_url, data=payload)
            response.raise_for_status()
            token_data = response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Refresh response did not return access_token.")

        self.access_token = access_token
        self.account_id = extract_account_id_from_jwt(access_token) or self.account_id

        new_refresh_token = token_data.get("refresh_token")
        if new_refresh_token:
            self.refresh_token = new_refresh_token

        updates: Dict[str, str] = {"CODEX_ACCESS_TOKEN": self.access_token}
        if self.account_id:
            updates["CODEX_ACCOUNT_ID"] = self.account_id
        if self.refresh_token:
            updates["CODEX_OAUTH_REFRESH_TOKEN"] = self.refresh_token

        expires_in = token_data.get("expires_in")
        if expires_in:
            self.expires_at = int(time.time() + int(expires_in))
            updates["CODEX_TOKEN_EXPIRES_AT"] = str(self.expires_at)

        persist_codex_state(self._auth_config.state_path, updates)

    @staticmethod
    def _parse_unix_ts(raw: Optional[str]) -> Optional[int]:
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
