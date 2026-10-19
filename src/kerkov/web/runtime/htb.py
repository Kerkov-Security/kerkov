"""HTB App Token setup outside the conversation and model history."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from kerkov.core.credentials import credential_spec_for_env_var
from kerkov.core.htb_api import BASE_URL, USER_INFO_PATH, request_headers, unwrap_user
from kerkov.web.runtime.providers import REQUEST_ID_PATTERN

HTB_TOKEN = credential_spec_for_env_var("HTB_API_TOKEN")
HTB_USER_URL = f"{BASE_URL}{USER_INFO_PATH}"


async def handle_htb_message(session: Any, content: dict[str, Any]) -> None:
    request_id = content.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        session.bus.emit("htb.error", {"detail": "Invalid HTB request ID."})
        return

    def emit(kind: str, **payload: Any) -> None:
        session.bus.emit(kind, {"request_id": request_id, **payload})

    operation = content.get("type")
    allowed = {"type", "request_id"}
    if operation == "htb.configure":
        allowed.add("token")
    if operation not in {"htb.status", "htb.configure"} or set(content) - allowed:
        emit("htb.error", detail="Invalid HTB request.")
        return
    if operation == "htb.status":
        configured = await asyncio.to_thread(session.credentials.get, HTB_TOKEN)
        emit("htb.status", configured=bool(configured))
        return

    token = content.get("token")
    if (
        not isinstance(token, str)
        or not 1 <= len(token.strip()) <= 4096
        or any(ord(char) < 33 or ord(char) == 127 for char in token.strip())
    ):
        emit("htb.error", detail="Enter a valid HTB App Token.")
        return
    token = token.strip()
    environment_token = os.environ.get("HTB_API_TOKEN")
    if environment_token and environment_token != token:
        emit(
            "htb.error",
            detail="HTB_API_TOKEN in the server environment overrides saved tokens for lab tools. Update or remove it, restart Kerkov, and retry.",
        )
        return
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                HTB_USER_URL,
                headers=request_headers(token),
            )
        if response.status_code == 401:
            emit(
                "htb.error", detail="HTB rejected this token. Generate a new App Token."
            )
            return
        if response.status_code != 200:
            emit("htb.error", detail="HTB could not verify the token. Try again later.")
            return
        user = unwrap_user(response.json())
        if not user.get("id"):
            emit("htb.error", detail="HTB returned an unexpected account response.")
            return
    except (httpx.HTTPError, ValueError):
        emit(
            "htb.error", detail="Unable to reach HTB or verify its response. Try again."
        )
        return

    # HTB skills run in subprocesses, which need the shared persistent store.
    result = await asyncio.to_thread(session.credentials.save, HTB_TOKEN, token)
    if not result.persisted:
        session.credentials.forget_session(HTB_TOKEN)
        emit(
            "htb.error",
            detail="Token verified, but could not be saved. Check your credential store and retry.",
        )
        return
    emit(
        "htb.configured",
        configured=True,
        detail="HTB connected. App Token saved for lab tools and future sessions.",
    )
