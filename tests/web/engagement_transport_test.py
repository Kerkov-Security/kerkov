"""Navigation reserves admission before its background task starts."""

import asyncio
import json
from unittest.mock import patch

import pytest

from kerkov.web.runtime.session import WebKerkovSession
from tests.web.engagement_runtime_test import saved
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("autonomy", [None, 2, 4])
async def test_legacy_conversations_list_and_resume_with_approval_guard(
    runtime, autonomy
):
    socket = _communicator()
    assert (await socket.connect())[0]
    try:
        await _until(socket, "session.ready")
        store, ident = saved(runtime[0])
        path = store.store_dir / f"{ident}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata = json.loads(lines[0])
        del metadata["verbosity"], metadata["approval_mode"]
        if autonomy is not None:
            metadata["autonomy"] = autonomy
        lines[0] = json.dumps(metadata)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        original = path.read_bytes()

        await socket.send_json_to({"type": "engagement.list", "request_id": "list"})
        listing = await _until(socket, "engagement.list")
        assert listing["engagements"][0]["id"] == ident
        expected_approval = "yolo" if autonomy == 4 else "normal"
        assert listing["engagements"][0]["approval_mode"] == expected_approval
        await socket.send_json_to(
            {
                "type": "engagement.resume",
                "request_id": "resume",
                "context_id": listing["context_id"],
                "engagement_id": ident,
            }
        )
        if autonomy == 4:
            result = await _until(socket, "engagement.error")
            assert "ALLOW_AUTONOMOUS" in result["detail"]
            assert runtime[0].engagement_store.engagement_id is None
        else:
            result = await _until(socket, "engagement.resumed")
            assert result["engagement_id"] == ident
            assert result["approval_mode"] == expected_approval
            assert result["verbosity"] == "standard"
            assert result["messages"][-1]["content"] == "Saved finding"
        assert path.read_bytes() == original
    finally:
        await socket.disconnect()


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
