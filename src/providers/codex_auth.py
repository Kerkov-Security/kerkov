import base64
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from src.core.config_manager import ConfigManager

DEFAULT_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_CODEX_SCOPE = "openid profile email offline_access"
DEFAULT_CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"


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


def find_env_path(start_dir: Path) -> Optional[Path]:
    current = start_dir
    for _ in range(4):
        candidate = current / ".env"
        if candidate.exists():
            return candidate
        current = current.parent
    return None


def upsert_env_values(env_path: Path, updates: Dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key, _, _value = line.partition("=")
        key = key.strip()
        if key in remaining:
            new_lines.append(f"{key}={remaining.pop(key)}")
        else:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class CodexTokenManager:
    """Handles Codex access token lifecycle (read, refresh, persist)."""

    def __init__(
        self,
        config: ConfigManager,
        access_token: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        self._config = config
        self.access_token = access_token or config.codex_access_token
        self.refresh_token = config.codex_oauth_refresh_token
        self.account_id = account_id or config.codex_account_id
        if not self.account_id and self.access_token:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        self.expires_at = self._parse_unix_ts(config.codex_token_expires_at)

    def ensure_valid_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            self._refresh_access_token()
        elif not self.access_token and self.refresh_token:
            self._refresh_access_token()
        elif self._is_expiring_soon() and self.refresh_token:
            self._refresh_access_token()

        if not self.access_token:
            raise ValueError("Missing Codex access token. Run /codex-login.")

        if not self.account_id:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        if not self.account_id:
            raise ValueError(
                "Missing ChatGPT account id in token. Re-run /codex-login to refresh CODEX_ACCESS_TOKEN."
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
            raise ValueError("Missing CODEX_OAUTH_REFRESH_TOKEN. Run /codex-login.")

        token_url = self._config.codex_oauth_token_url or DEFAULT_CODEX_TOKEN_URL
        client_id = self._config.codex_oauth_client_id or DEFAULT_CODEX_CLIENT_ID

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": client_id,
        }
        if self._config.codex_oauth_client_secret:
            payload["client_secret"] = self._config.codex_oauth_client_secret

        with httpx.Client(timeout=self._config.max_timeout_seconds) as client:
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

        env_path = find_env_path(Path.cwd())
        if env_path is not None:
            upsert_env_values(env_path, updates)

        for key, value in updates.items():
            os.environ[key] = value

    @staticmethod
    def _parse_unix_ts(raw: Optional[str]) -> Optional[int]:
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
