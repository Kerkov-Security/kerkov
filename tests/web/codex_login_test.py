"""Local Codex callbacks keep credentials private and sign-ins cancellable."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from kerkov.providers.codex import oauth
from kerkov.providers.codex.auth import (
    CodexAuthConfig,
    CodexTokenManager,
    load_codex_state,
)
from kerkov.web.runtime import codex_login
from tests.web.provider_runtime_test import request, session


def _create_mock_token_client():
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "test-account"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.json.return_value = {
        "access_token": token,
        "refresh_token": "private-refresh-token",
        "expires_in": 3600,
    }
    return client, token


@pytest.fixture
def login_session(session, unused_tcp_port):
    session.config = session.config.model_copy(
        update={
            "codex_oauth_redirect_uri": f"http://127.0.0.1:{unused_tcp_port}/auth/callback",
            "codex_oauth_callback_bind_host": "127.0.0.1",
        }
    )
    return session


async def _next(session):
    return await asyncio.wait_for(session.bus.next_event(), timeout=3)


async def _callback(session, **params):
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.get(session.config.codex_oauth_redirect_uri, params=params)


async def _start(session):
    session.bus.drain_nowait()
    task = asyncio.create_task(
        session.handle_provider_message(
            {"type": "provider.login", "request_id": "login-1", "provider": "codex"}
        )
    )
    event = await _next(session)
    assert event["type"] == "provider.login_ready", event
    return task, parse_qs(urlsplit(event["payload"]["url"]).query), event


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_existing", [False, True])
async def test_login_validates_state_exchanges_pkce_and_saves_without_switching(
    login_session, monkeypatch, reuse_existing
):
    session = login_session
    client, token = _create_mock_token_client()
    monkeypatch.setattr(oauth.httpx, "Client", lambda **kwargs: client)
    original = session.provider
    existing = None
    if reuse_existing:
        existing = CodexTokenManager(
            CodexAuthConfig(
                Path(session.config.codex_state_path),
                "https://example.invalid",
                "client",
                None,
                1,
            ),
            access_token="old-access",
            refresh_token="old-refresh",
            account_id="old-account",
        )
        session.providers._codex_token_manager = existing
    task, params, ready = await _start(session)
    try:
        # Neither a stray callback nor a forged denial may consume the login.
        response = await _callback(session, code="stray-code", state="wrong-state")
        assert "Invalid OAuth state" in response.text
        await _callback(session, error="access_denied", state="wrong-state")
        assert not task.done()
        assert not Path(session.config.codex_state_path).exists()
        response = await _callback(
            session, code="private-authorization-code", state=params["state"][0]
        )
        assert response.headers["cache-control"] == "no-store"
        assert "private-authorization-code" not in response.text
        await asyncio.wait_for(task, 3)
        saved = await _next(session)
        assert saved["type"] == "provider.configured"
        assert saved["payload"] == {
            "request_id": "login-1",
            "provider": "codex",
            "persisted": True,
        }
        form = client.post.call_args.kwargs["data"]
        assert form["code"] == "private-authorization-code"
        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert params["code_challenge"] == [challenge]
        assert params["code_challenge_method"] == ["S256"]
        state_path = Path(session.config.codex_state_path)
        assert load_codex_state(state_path)["CODEX_ACCESS_TOKEN"] == token
        if os.name != "nt":
            assert state_path.stat().st_mode & 0o777 == 0o600
        assert session.provider is original
        assert not session.providers.switching
        for value in (
            token,
            "private-refresh-token",
            "private-authorization-code",
            form["code_verifier"],
        ):
            assert value not in json.dumps([ready, saved])
        candidate = session.providers._build_candidate("codex")
        assert candidate.token_manager is session.providers._codex_token_manager
        assert candidate.token_manager.access_token == token
        if existing is not None:
            assert candidate.token_manager is existing
            assert existing.refresh_token == "private-refresh-token"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "disconnect", "deny"])
async def test_cancel_disconnect_and_denial_release_listener_without_saving(
    login_session, finish
):
    session = login_session
    task, params, _ = await _start(session)
    try:
        if finish == "cancel":
            await session.handle_provider_message(
                {
                    "type": "provider.login_cancel",
                    "request_id": "stale",
                    "provider": "codex",
                }
            )
            assert not session.providers.codex_login.cancelled.is_set()
            await session.handle_provider_message(
                {
                    "type": "provider.login_cancel",
                    "request_id": "login-1",
                    "provider": "codex",
                }
            )
        elif finish == "disconnect":
            session.close()
            task.cancel()
        else:
            await _callback(
                session, error="private-denial-text", state=params["state"][0]
            )
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        if finish != "disconnect":
            event = await _next(session)
            assert event["type"] == "provider.selection_error"
            assert event["payload"]["operation"] == "login"
            assert "private-denial-text" not in str(event)
        assert not Path(session.config.codex_state_path).exists()
        assert not session.providers.switching
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(
                ("127.0.0.1", urlsplit(session.config.codex_oauth_redirect_uri).port)
            )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_and_retry_do_not_leave_callback_port_busy(
    login_session, monkeypatch
):
    monkeypatch.setattr(codex_login, "LOGIN_TIMEOUT_SECONDS", 0)
    for _ in range(2):
        task, _, _ = await _start(login_session)
        await asyncio.wait_for(task, 3)
        event = await _next(login_session)
        assert "timed out" in event["payload"]["detail"]
    assert not Path(login_session.config.codex_state_path).exists()


@pytest.mark.asyncio
async def test_busy_callback_port_returns_safe_error_without_sign_in_link(
    login_session,
):
    port = urlsplit(login_session.config.codex_oauth_redirect_uri).port
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        event = await request(login_session, "login", provider="codex")
    assert event["type"] == "provider.selection_error"
    assert "port is in use" in event["payload"]["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_login_admission_and_secret_inputs_are_guarded(login_session, pending):
    if not pending:
        await login_session._lock.acquire()
    try:
        event = await request(login_session, "login", provider="codex", pending=pending)
        assert event["type"] == "provider.selection_error"
        assert "Wait for" in event["payload"]["detail"]
    finally:
        if not pending:
            login_session._lock.release()
    event = await request(login_session, "login", provider="codex", code="private-code")
    assert event["type"] == "provider.selection_error"
    assert "private-code" not in str(event)


@pytest.mark.asyncio
async def test_exchange_failure_does_not_save_or_expose_response_body(
    login_session, monkeypatch
):
    client, _ = _create_mock_token_client()
    client.post.side_effect = RuntimeError("private-code private-token https://secret")
    monkeypatch.setattr(oauth.httpx, "Client", lambda **kwargs: client)
    task, params, _ = await _start(login_session)
    try:
        await _callback(login_session, code="private-code", state=params["state"][0])
        await asyncio.wait_for(task, 3)
        event = await _next(login_session)
        assert event["type"] == "provider.selection_error"
        assert "private" not in str(event)
        assert "secret" not in str(event)
        assert not Path(login_session.config.codex_state_path).exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_during_token_exchange_prevents_late_save(
    login_session, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()

    def exchange(*args):
        entered.set()
        assert release.wait(3)
        return {
            "CODEX_ACCESS_TOKEN": "private-access",
            "CODEX_OAUTH_REFRESH_TOKEN": "private-refresh",
            "CODEX_ACCOUNT_ID": "test-account",
        }

    monkeypatch.setattr(oauth, "exchange_codex_oauth_code", exchange)
    task, params, _ = await _start(login_session)
    try:
        await _callback(login_session, code="private-code", state=params["state"][0])
        assert await asyncio.to_thread(entered.wait, 1)
        await login_session.handle_provider_message(
            {
                "type": "provider.login_cancel",
                "request_id": "login-1",
                "provider": "codex",
            }
        )
        release.set()
        await asyncio.wait_for(task, 3)
        event = await _next(login_session)
        assert event["type"] == "provider.selection_error"
        assert "cancelled" in event["payload"]["detail"]
        assert not Path(login_session.config.codex_state_path).exists()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
