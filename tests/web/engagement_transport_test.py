"""Navigation reserves admission before its background task starts."""

import asyncio
from unittest.mock import patch

import pytest

from kerkov.web.runtime.session import WebKerkovSession
from tests.web.engagement_runtime_test import saved
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.mark.asyncio
async def test_list_and_resume_round_trip(runtime):
    socket = _communicator()
    assert (await socket.connect())[0]
    try:
        await _until(socket, "session.ready")
        _, ident = saved(runtime[0])
        await socket.send_json_to({"type": "engagement.list", "request_id": "list"})
        listing = await _until(socket, "engagement.list")
        assert listing["engagements"][0]["id"] == ident
        await socket.send_json_to(
            {
                "type": "engagement.resume",
                "request_id": "resume",
                "context_id": listing["context_id"],
                "engagement_id": ident,
            }
        )
        result = await _until(socket, "engagement.resumed")
        assert result["engagement_id"] == ident
        assert result["messages"][-1]["content"] == "Saved finding"
    finally:
        await socket.disconnect()


@pytest.mark.asyncio
async def test_navigation_blocks_chat_provider_and_inventory_but_not_approvals(runtime):
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(session, content, *, message_pending=False):
        started.set()
        await release.wait()

    with patch.object(WebKerkovSession, "handle_engagement_message", handle):
        socket = _communicator()
        assert (await socket.connect())[0]
        try:
            await _until(socket, "session.ready")
            await socket.send_json_to(
                {"type": "engagement.resume", "request_id": "open"}
            )
            await started.wait()
            await socket.send_json_to(
                {"type": "user.message", "message": "hello", "message_id": "chat"}
            )
            assert (
                "engagement"
                in (await _until(socket, "user.message.rejected"))["detail"]
            )
            await socket.send_json_to(
                {"type": "provider.switch", "request_id": "switch"}
            )
            await _until(socket, "provider.selection_error")
            await socket.send_json_to(
                {"type": "inventory.list", "request_id": "inventory"}
            )
            inventory = await _until(socket, "inventory.snapshot")
            await socket.send_json_to(
                {
                    "type": "inventory.save",
                    "request_id": "save",
                    "context_id": inventory["context_id"],
                    "section": "assets",
                    "resource": {"kind": "domain", "value": "example.com"},
                }
            )
            assert "Wait" in (await _until(socket, "inventory.error"))["detail"]
            await socket.send_json_to(
                {"type": "approval.response", "approval_id": "none", "decision": "deny"}
            )
            await _until(socket, "approval.rejected")
        finally:
            release.set()
            await socket.disconnect()
