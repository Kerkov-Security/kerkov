"""Shared Codex refreshes must not block async turns or their cancellation."""

import asyncio
import threading
import time
from unittest.mock import Mock

import pytest

from kerkov.models.basic_models import Message
from kerkov.providers.codex.auth import CodexAuthConfig, CodexTokenManager
from kerkov.providers.codex.provider import CodexProvider
from tests.providers.codex_provider_test import _FakeAsyncClient, _FakeStreamResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["shared_catalog_lock", "unauthorized_retry"])
async def test_auth_wait_keeps_loop_responsive_and_cancels_without_late_request(
    tmp_path, monkeypatch, phase
):
    manager = CodexTokenManager(
        CodexAuthConfig(
            tmp_path / "auth.json", "https://example.invalid", "client", None, 1
        ),
        access_token="old-token",
        refresh_token="refresh-token",
        account_id="test-account",
        expires_at=str(
            1 if phase == "shared_catalog_lock" else int(time.time()) + 3600
        ),
    )
    provider = CodexProvider(
        model="gpt-test",
        base_url="https://example.invalid",
        access_token="old-token",
        account_id="test-account",
    )
    provider.token_manager = manager
    refresh_started = threading.Event()
    release = threading.Event()
    auth_started = threading.Event()
    auth_finished = threading.Event()

    def refresh():
        refresh_started.set()
        assert release.wait(3)
        manager.access_token = "refreshed-token"
        manager.refresh_token = "rotated-refresh-token"
        manager.expires_at = int(time.time()) + 3600

    original_auth = provider._ensure_auth

    def auth(force_refresh=False):
        # In the retry case the first non-refresh auth check should finish.
        blocked_check = phase == "shared_catalog_lock" or force_refresh
        if blocked_check:
            auth_started.set()
        try:
            original_auth(force_refresh=force_refresh)
        finally:
            if blocked_check:
                auth_finished.set()

    monkeypatch.setattr(manager, "_refresh_access_token", refresh)
    monkeypatch.setattr(provider, "_ensure_auth", auth)
    client = _FakeAsyncClient(_FakeStreamResponse(["data: [DONE]"]))
    responses = (
        [_FakeStreamResponse([], status_code=401), client._response]
        if phase == "unauthorized_retry"
        else [client._response]
    )
    client.stream = Mock(side_effect=responses)
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(
        "kerkov.providers.codex.provider.httpx.AsyncClient", client_factory
    )

    catalog = None
    turn = None
    # A regressed event-loop-blocking implementation still releases and fails,
    # instead of hanging the test process while waiting for the refresh lock.
    failsafe = threading.Timer(2, release.set)
    failsafe.start()
    try:
        if phase == "shared_catalog_lock":
            catalog = asyncio.create_task(asyncio.to_thread(manager.ensure_valid_token))
            assert await asyncio.to_thread(refresh_started.wait, 1)
        turn = asyncio.create_task(
            provider.complete([Message(role="user", content="continue")])
        )
        assert await asyncio.to_thread(auth_started.wait, 1)
        assert await asyncio.to_thread(refresh_started.wait, 1)
        # This assertion runs while the lock/HTTP work remains blocked. Reaching
        # it proves the same loop can handle approval and cancellation events.
        assert not release.is_set()
        assert not turn.done()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, 0.5)
        expected_requests = int(phase == "unauthorized_retry")
        assert client.stream.call_count == expected_requests
        if phase == "shared_catalog_lock":
            client_factory.assert_not_called()

        release.set()
        if catalog is not None:
            assert await catalog == "refreshed-token"
        assert await asyncio.to_thread(auth_finished.wait, 1)
        assert client.stream.call_count == expected_requests
        assert manager.refresh_token == "rotated-refresh-token"

        # The refresh may finish after cancellation. Its rotated credentials
        # stay usable for the next turn, without reviving the canceled request.
        await provider.complete([Message(role="user", content="next turn")])
        headers = client.stream.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer refreshed-token"
        assert provider.api_key == "refreshed-token"
    finally:
        release.set()
        failsafe.cancel()
        if turn is not None:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        if catalog is not None:
            await asyncio.gather(catalog, return_exceptions=True)
        if auth_started.is_set():
            await asyncio.to_thread(auth_finished.wait, 1)
