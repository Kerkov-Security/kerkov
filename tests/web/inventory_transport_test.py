"""WebSocket routing and turn admission for inventory requests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from channels.testing import WebsocketCommunicator
from django.conf import settings

from kerkov.core.config_manager import ConfigManager
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.web.project.asgi import application
from kerkov.web.runtime.session import WebKerkovSession

django.setup()


def _communicator() -> WebsocketCommunicator:
    cookie = (
        f"{settings.KERKOV_WEB_ACCESS_COOKIE_NAME}={settings.KERKOV_WEB_ACCESS_TOKEN}"
    )
    return WebsocketCommunicator(
        application,
        "/ws/console/",
        headers=[
            (b"host", b"localhost:8764"),
            (b"origin", b"http://localhost:8764"),
            (b"cookie", cookie.encode("ascii")),
        ],
    )


@pytest.fixture
def runtime(tmp_path: Path):
    sessions: list[WebKerkovSession] = []

    def build(bus):
        session = WebKerkovSession(
            bus,
            config=ConfigManager(
                GOOGLE_API_KEY="test",
                KB_ENABLED=True,
                EXECUTION_ENABLED=False,
                WORKSPACE_DIR=str(tmp_path / "workspaces"),
                USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
                HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
            ),
        )
        sessions.append(session)
        return session

    async def initialize(session):
        session._emit_ready(provider_ready=False)

    with (
        patch("kerkov.web.console.consumers.WebKerkovSession", side_effect=build),
        patch.object(WebKerkovSession, "initialize", initialize),
    ):
        yield sessions


async def _until(communicator: WebsocketCommunicator, event_type: str) -> dict:
    while True:
        event = await communicator.receive_json_from()
        if event["type"] == event_type:
            return event["payload"]


@pytest.mark.asyncio
async def test_inventory_round_trip_before_first_message(runtime) -> None:
    communicator = _communicator()
    connected, _ = await communicator.connect()
    assert connected
    try:
        await _until(communicator, "session.ready")
        await communicator.send_json_to(
            {"type": "inventory.list", "request_id": "initial"}
        )
        initial = await _until(communicator, "inventory.snapshot")
        assert initial["request_id"] == "initial"
        assert initial["engagement_id"] is None
        assert initial["graph"] == {"nodes": [], "edges": []}
        await communicator.send_json_to(
            {
                "type": "inventory.save",
                "request_id": "add",
                "context_id": initial["context_id"],
                "entry_id": None,
                "section": "targets",
                "resource": {"kind": "url", "value": "https://Example.COM/Case"},
            }
        )
        saved = await _until(communicator, "inventory.saved")
        assert saved["request_id"] == "add"
        snapshot = await _until(communicator, "inventory.snapshot")
        assert snapshot["entries"][0]["entry"]["value"] == "https://example.com/Case"
        assert snapshot["context_id"] == initial["context_id"]
        assert len(snapshot["graph"]["nodes"]) == 1
        node = snapshot["graph"]["nodes"][0]
        assert node["kind"] == "url"
        assert node["value"] == "https://example.com/Case"
        assert node["roles"] == ["target"]
        assert node["entry_ids"] == [saved["entry_id"]]
        assert snapshot["graph"]["edges"] == []
        await communicator.send_json_to(
            {
                "type": "inventory.evidence",
                "request_id": "evidence",
                "context_id": snapshot["context_id"],
                "entry_id": saved["entry_id"],
            }
        )
        error = await _until(communicator, "inventory.error")
        assert error["operation"] == "evidence"
        assert error["request_id"] == "evidence"
        assert "no linked evidence" in error["detail"]
        assert runtime[0].conversation_history[0].role == "system"
    finally:
        await communicator.disconnect()
    assert runtime[0]._closed is True
    assert runtime[0].knowledge_base is None


@pytest.mark.asyncio
async def test_refresh_transports_recorded_reachability_from_named_origin(
    runtime,
) -> None:
    communicator = _communicator()
    connected, _ = await communicator.connect()
    assert connected
    try:
        await _until(communicator, "session.ready")
        await communicator.send_json_to(
            {"type": "inventory.list", "request_id": "initial"}
        )
        initial = await _until(communicator, "inventory.snapshot")
        await communicator.send_json_to(
            {
                "type": "inventory.save",
                "request_id": "add",
                "context_id": initial["context_id"],
                "section": "targets",
                "resource": {"kind": "domain", "value": "api.example.com"},
            }
        )
        saved = await _until(communicator, "inventory.saved")
        await _until(communicator, "inventory.snapshot")
        ledger = runtime[0].findings_ledger
        assert ledger is not None
        connection_id = await ledger.add(
            LedgerSection.CONNECTIONS,
            {
                "source_vantage": "assessment-container",
                "target_id": saved["entry_id"],
                "relation": "reaches",
                "protocol": "tcp",
                "port": 8443,
                "service": "HTTPS",
                "provenance": "declared",
                "note": "Configured access from the assessment container.",
            },
            engagement_id="agent",
        )
        await communicator.send_json_to(
            {"type": "inventory.list", "request_id": "routes"}
        )
        snapshot = await _until(communicator, "inventory.snapshot")
        assert snapshot["request_id"] == "routes"
        assert snapshot["context_id"] == initial["context_id"]
        assert len(snapshot["entries"]) == 1
        nodes = {node["id"]: node for node in snapshot["graph"]["nodes"]}
        [edge] = snapshot["graph"]["edges"]
        assert nodes[edge["source"]]["kind"] == "vantage"
        assert nodes[edge["source"]]["value"] == "assessment-container"
        assert nodes[edge["target"]]["value"] == "api.example.com"
        assert (edge["relation"], edge["protocol"], edge["port"]) == (
            "reaches",
            "tcp",
            8443,
        )
        assert edge["service"] == "HTTPS"
        assert edge["provenances"] == ["declared"]
        assert edge.get("observed_at") is None
        assert edge["connection_ids"] == [connection_id]
    finally:
        await communicator.disconnect()


@pytest.mark.asyncio
async def test_inventory_list_does_not_wait_for_chat_and_save_rejects_pending_turn(
    runtime,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def handle_message(session, message, **kwargs):
        # Deliberately do not acquire the session lock: consumer admission must
        # account for an accepted task before that task starts its engine turn.
        started.set()
        await release.wait()

    with patch.object(WebKerkovSession, "handle_user_message", handle_message):
        communicator = _communicator()
        connected, _ = await communicator.connect()
        assert connected
        try:
            await _until(communicator, "session.ready")
            await communicator.send_json_to(
                {"type": "user.message", "message_id": "chat", "message": "hello"}
            )
            await _until(communicator, "user.message.accepted")
            await asyncio.wait_for(started.wait(), 1)
            await communicator.send_json_to(
                {"type": "inventory.list", "request_id": "during-chat"}
            )
            snapshot = await _until(communicator, "inventory.snapshot")
            assert snapshot["available"] is True
            await communicator.send_json_to(
                {
                    "type": "inventory.save",
                    "request_id": "blocked",
                    "context_id": snapshot["context_id"],
                    "section": "assets",
                    "resource": {"kind": "domain", "value": "example.com"},
                }
            )
            error = await _until(communicator, "inventory.error")
            assert error["operation"] == "save"
            assert "finish" in error["detail"]
            assert runtime[0].engagement_store.engagement_id is None
            await communicator.send_json_to(
                {
                    "type": "approval.response",
                    "approval_id": "missing",
                    "decision": "deny",
                }
            )
            assert (await communicator.receive_json_from())[
                "type"
            ] == "approval.rejected"
            release.set()
            await _until(communicator, "user.message.complete")
        finally:
            await communicator.disconnect()


@pytest.mark.asyncio
async def test_profile_transport_uses_current_engagement_evidence(runtime):
    communicator = _communicator()
    connected, _ = await communicator.connect()
    assert connected
    try:
        await _until(communicator, "session.ready")
        await communicator.send_json_to(
            {"type": "inventory.list", "request_id": "list"}
        )
        snapshot = await _until(communicator, "inventory.snapshot")
        await communicator.send_json_to(
            {
                "type": "inventory.save",
                "request_id": "save",
                "context_id": snapshot["context_id"],
                "entry_id": None,
                "section": "targets",
                "resource": {"kind": "domain", "value": "example.com"},
            }
        )
        saved = await _until(communicator, "inventory.saved")
        await _until(communicator, "inventory.snapshot")
        web = runtime[0]
        artifact_id = await web.knowledge_base.add_artifact(
            engagement_id=web.engagement_store.engagement_id,
            title="TLS",
            body="TLS evidence",
        )
        await web.findings_ledger.add(
            LedgerSection.FINDINGS,
            {"title": "Legacy TLS", "host": "example.com"},
            engagement_id=web.engagement_store.engagement_id,
            artifact_ref=artifact_id,
        )
        request = {
            "type": "inventory.profile",
            "request_id": "profile",
            "context_id": snapshot["context_id"],
            "entry_id": saved["entry_id"],
        }
        await communicator.send_json_to(request)
        profile = await _until(communicator, "inventory.profile")
        assert profile["profile"]["findings"][0]["entry"]["title"] == "Legacy TLS"
        await communicator.send_json_to(
            {
                **request,
                "type": "inventory.profile_evidence",
                "request_id": "evidence",
                "artifact_id": artifact_id,
            }
        )
        evidence = await _until(communicator, "inventory.profile_evidence")
        assert evidence["artifact"]["body"] == "TLS evidence"
    finally:
        await communicator.disconnect()
