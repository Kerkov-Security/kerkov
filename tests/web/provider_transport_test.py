"""Provider requests stay responsive and serialize mutations with chat turns."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.web.runtime.session import WebKerkovSession
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["catalog", "models", "switch", "settings", "test", "configure", "default"],
)
async def test_provider_request_routes_with_correlation(runtime, operation) -> None:
    seen = []

    async def handle(session, content, *, message_pending=False):
        seen.append((content, message_pending))
        session.bus.emit("provider.test", {"request_id": content["request_id"]})

    with patch.object(WebKerkovSession, "handle_provider_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            request = {
                "type": f"provider.{operation}",
                "request_id": "selection-1",
                "provider": "ollama",
                "model": "local-model",
            }
            await communicator.send_json_to(request)
            assert await _until(communicator, "provider.test") == {
                "request_id": "selection-1"
            }
            assert seen == [(request, False)]
        finally:
            await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["switch", "configure", "default"])
async def test_switch_reserves_turn_and_inventory_admission_without_blocking_approvals(
    runtime,
    operation,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(session, content, *, message_pending=False):
        # No runtime lock: admission must protect even a just-scheduled switch.
        started.set()
        await release.wait()
        session.bus.emit("provider.changed", {"request_id": content["request_id"]})

    with patch.object(WebKerkovSession, "handle_provider_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            await communicator.send_json_to(
                {"type": f"provider.{operation}", "request_id": "switch-1"}
            )
            await asyncio.wait_for(started.wait(), 1)
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "chat", "message": "hello"}
            )
            rejected = await _until(communicator, "user.message.rejected")
            assert rejected["message_id"] == "chat"
            assert "provider" in rejected["detail"]
            await communicator.send_json_to(
                {"type": "provider.switch", "request_id": "switch-2"}
            )
            rejected = await _until(communicator, "provider.selection_error")
            assert rejected["request_id"] == "switch-2"
            assert rejected["operation"] == "switch"
            await communicator.send_json_to(
                {"type": "inventory.list", "request_id": "inventory"}
            )
            snapshot = await _until(communicator, "inventory.snapshot")
            await communicator.send_json_to(
                {
                    "type": "inventory.save",
                    "request_id": "save",
                    "context_id": snapshot["context_id"],
                    "section": "assets",
                    "resource": {"kind": "domain", "value": "example.com"},
                }
            )
            assert (await _until(communicator, "inventory.error"))[
                "operation"
            ] == "save"
            assert runtime[0].engagement_store.engagement_id is None
            await communicator.send_json_to(
                {
                    "type": "approval.response",
                    "approval_id": "missing",
                    "decision": "deny",
                }
            )
            await _until(communicator, "approval.rejected")
            release.set()
            await _until(communicator, "provider.changed")
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "after", "message": "/mode"}
            )
            assert (await _until(communicator, "user.message.accepted"))[
                "message_id"
            ] == "after"
            await _until(communicator, "user.message.complete")
        finally:
            await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["switch", "configure", "default"])
async def test_provider_dispatch_sees_accepted_parent_turn(runtime, operation) -> None:
    release = asyncio.Event()

    async def message(session, content):
        await release.wait()

    async def provider(session, content, *, message_pending=False):
        session.bus.emit("provider.test", {"message_pending": message_pending})

    with (
        patch.object(WebKerkovSession, "handle_user_message", message),
        patch.object(WebKerkovSession, "handle_provider_message", provider),
    ):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "chat", "message": "hello"}
            )
            await _until(communicator, "user.message.accepted")
            await communicator.send_json_to(
                {"type": f"provider.{operation}", "request_id": "switch"}
            )
            assert (await _until(communicator, "provider.test"))[
                "message_pending"
            ] is True
        finally:
            await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "test", "configure", "default"])
async def test_provider_request_limit_and_disconnect_cancel_pending_work(
    runtime,
    operation,
) -> None:
    cancelled = []

    async def handle(session, content, *, message_pending=False):
        session.bus.emit("provider.started", {"request_id": content["request_id"]})
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(content["request_id"])

    with patch.object(WebKerkovSession, "handle_provider_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            # Mutations reserve one slot; read-only requests can fill the rest.
            for index in range(4):
                await communicator.send_json_to(
                    {
                        "type": (
                            f"provider.{operation}" if index == 0 else "provider.models"
                        ),
                        "request_id": str(index),
                    }
                )
                await _until(communicator, "provider.started")
            await communicator.send_json_to(
                {"type": "provider.models", "request_id": "overflow"}
            )
            error = await _until(communicator, "provider.selection_error")
            assert error["request_id"] == "overflow"
            assert error["operation"] == "models"
        finally:
            await communicator.disconnect()
        assert sorted(cancelled) == ["0", "1", "2", "3"]
        assert runtime[0]._closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["switch", "configure", "default"])
async def test_unexpected_provider_error_is_sanitized_and_releases_admission(
    runtime,
    operation,
) -> None:
    async def handle(session, content, *, message_pending=False):
        raise RuntimeError("secret-provider-key")

    with patch.object(WebKerkovSession, "handle_provider_message", handle):
        communicator = _communicator()
        assert (await communicator.connect())[0]
        try:
            await _until(communicator, "session.ready")
            await communicator.send_json_to(
                {"type": f"provider.{operation}", "request_id": "switch"}
            )
            error = await _until(communicator, "provider.selection_error")
            assert "secret-provider-key" not in str(error)
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "after", "message": "/mode"}
            )
            await _until(communicator, "user.message.accepted")
            await _until(communicator, "user.message.complete")
        finally:
            await communicator.disconnect()
