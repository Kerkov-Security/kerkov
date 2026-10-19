"""Shared HTB API conventions for the console and bundled lab helpers."""

from typing import Any

BASE_URL = "https://labs.hackthebox.com"
USER_INFO_PATH = "/api/v4/user/info"


def request_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "kerkov/0.1",
        "Accept": "application/json",
    }


def unwrap_user(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("info", "user", "data"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
        return payload
    return {}
