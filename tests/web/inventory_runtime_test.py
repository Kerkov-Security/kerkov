"""Inventory persistence, validation, evidence and engagement isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.inventory import MAX_EVIDENCE_CHARS
from kerkov.web.runtime.session import WebKerkovSession


@pytest.fixture
def session(tmp_path: Path):
    web = WebKerkovSession(
        WebEventBus(),
        config=ConfigManager(
            GOOGLE_API_KEY="test",
            KB_ENABLED=True,
            EXECUTION_ENABLED=False,
            WORKSPACE_DIR=str(tmp_path / "workspaces"),
            USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
            HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
        ),
    )
    yield web
    web.close()


async def _request(web: WebKerkovSession, operation: str, **kwargs) -> list[dict]:
    web.bus.drain_nowait()
    content = {
        "type": f"inventory.{operation}",
        "request_id": "request-1",
        "context_id": web.inventory.context_id,
        **kwargs,
    }
    await web.handle_inventory_message(content)
    return web.bus.drain_nowait()


async def _save(web: WebKerkovSession, **kwargs) -> dict:
    events = await _request(
        web,
        "save",
        **{
            "section": "assets",
            "resource": {"kind": "domain", "value": "Example.COM.", "name": "Main"},
            **kwargs,
        },
    )
    assert events[-2]["type"] == "inventory.saved", events
    assert events[-1]["type"] == "inventory.snapshot"
    return events[-1]["payload"]


@pytest.mark.asyncio
async def test_empty_inventory_stays_lazy_and_save_starts_without_provider(
    session: WebKerkovSession,
) -> None:
    events = await _request(session, "list")
    initial = events[-1]["payload"]
    assert initial["entries"] == []
    assert initial["engagement_id"] is None
    assert initial["available"] is True
    assert session.engagement_store.engagement_id is None
    assert not list(session.engagement_store.store_dir.glob("*.jsonl"))

    snapshot = await _save(session)
    assert snapshot["context_id"] == initial["context_id"]
    assert snapshot["engagement_id"] == session.engagement_store.engagement_id
    assert snapshot["entries"][0]["entry"] == {
        "kind": "domain",
        "value": "example.com",
        "name": "Main",
    }
    assert session.conversation_history[0].role == "system"
    session._apply_system_prompt_if_needed()
    assert session.engagement_store.engagement_id == snapshot["engagement_id"]
    session.close()
    kb = EngagementKnowledgeBase(
        snapshot["engagement_id"], session.engagement_store.store_dir
    )
    try:
        rows = await kb.list_ledger_entries(section=LedgerSection.ASSETS)
        assert rows[0].entry["value"] == "example.com"
    finally:
        kb.close()


@pytest.mark.asyncio
async def test_inventory_only_lists_resources_and_refreshes_agent_writes(
    session: WebKerkovSession,
) -> None:
    await _save(session)
    ledger = session.findings_ledger
    assert ledger is not None
    await ledger.add(
        LedgerSection.CREDENTIALS,
        {
            "username": "private-user",
            "secret": "not-for-inventory",
            "host": "example.com",
        },
        engagement_id="agent-1",
    )
    await ledger.add(
        LedgerSection.TARGETS,
        {"kind": "ip", "value": "192.0.2.1"},
        engagement_id="agent-1",
    )
    async with session._lock:
        events = await _request(session, "list")
    assert [row["section"] for row in events[-1]["payload"]["entries"]] == [
        "assets",
        "targets",
    ]
    assert "not-for-inventory" not in str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"section": "credentials"},
        {"section": []},
        {"resource": "example.com"},
        {"resource": {"kind": "domain", "value": 42}},
        {"resource": {"kind": "domain", "value": "x" * 8193}},
        {"resource": {"kind": "domain", "value": "example.com", "name": "x" * 257}},
        {"resource": {"kind": "domain", "value": "example.com", "name": False}},
        {"resource": {"kind": "domain", "value": "example.com", "artifact_ref": 1}},
        {"resource": {"kind": "domain", "value": "example.com\n"}},
        {"resource": {"kind": "url", "value": "https://secret:password@example.com/"}},
        {"resource": {"kind": "ip", "value": "raw-private-text"}},
        {"entry_id": True},
        {"entry_id": -1},
        {"entry_id": 2**63},
        {"expected_updated_at": 42},
        {"request_id": "x" * 129},
        {"request_id": []},
        {"context_id": "retired"},
    ],
)
async def test_invalid_inventory_requests_never_start_engagement(
    session: WebKerkovSession,
    bad: dict,
) -> None:
    events = await _request(
        session,
        "save",
        **{
            "section": "assets",
            "resource": {"kind": "domain", "value": "example.com"},
            **bad,
        },
    )
    assert events[-1]["type"] == "inventory.error"
    assert session.engagement_store.engagement_id is None
    assert "raw-private-text" not in str(events)
    assert "password" not in str(events)


@pytest.mark.asyncio
async def test_busy_saves_reject_and_lists_remain_available(
    session: WebKerkovSession,
) -> None:
    async with session._lock:
        events = await _request(
            session,
            "save",
            section="assets",
            resource={"kind": "ip", "value": "192.0.2.1"},
        )
        assert events[-1]["type"] == "inventory.error"
        assert "finish" in events[-1]["payload"]["detail"]
        events = await _request(session, "list")
        assert events[-1]["type"] == "inventory.snapshot"
    assert session.engagement_store.engagement_id is None


@pytest.mark.asyncio
async def test_edits_clear_names_and_preserve_only_matching_evidence(
    session: WebKerkovSession,
) -> None:
    snapshot = await _save(session)
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    artifact_id = await kb.add_artifact(
        engagement_id="agent-1", body="Evidence for example.com"
    )
    row_id = snapshot["entries"][0]["id"]
    await ledger.add(
        LedgerSection.ASSETS,
        {"kind": "domain", "value": "example.com"},
        engagement_id="agent-1",
        artifact_ref=artifact_id,
    )
    row = (await ledger.list(LedgerSection.ASSETS))[0]
    snapshot = await _save(
        session,
        entry_id=row_id,
        expected_updated_at=row.updated_at,
        section="targets",
        resource={"kind": "domain", "value": "example.com", "name": None},
    )
    assert len(snapshot["entries"]) == 1
    edited = snapshot["entries"][0]
    assert edited["section"] == "targets"
    assert "name" not in edited["entry"]
    assert edited["artifact_ref"] == artifact_id
    snapshot = await _save(
        session,
        entry_id=row_id,
        section="targets",
        resource={"kind": "domain", "value": "new.example.com"},
    )
    assert len(snapshot["entries"]) == 1
    assert snapshot["entries"][0]["entry"]["value"] == "new.example.com"
    assert snapshot["entries"][0]["artifact_ref"] is None


@pytest.mark.asyncio
async def test_edit_into_duplicate_keeps_destination_evidence_atomically(
    session: WebKerkovSession,
) -> None:
    original = (await _save(session))["entries"][0]
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    evidence = await kb.add_artifact(
        engagement_id="agent-1", body="Destination evidence"
    )
    destination_id = await ledger.add(
        LedgerSection.TARGETS,
        {"kind": "domain", "value": "other.example.com", "name": "Old name"},
        engagement_id="agent-1",
        artifact_ref=evidence,
    )
    snapshot = await _save(
        session,
        entry_id=original["id"],
        section="targets",
        resource={"kind": "domain", "value": "other.example.com"},
    )
    assert len(snapshot["entries"]) == 1
    result = snapshot["entries"][0]
    assert result["id"] == destination_id
    assert result["artifact_ref"] == evidence
    assert "name" not in result["entry"]


@pytest.mark.asyncio
async def test_stale_edits_and_nonresource_ids_leave_rows_unchanged(
    session: WebKerkovSession,
) -> None:
    row = (await _save(session))["entries"][0]
    ledger = session.findings_ledger
    assert ledger is not None
    await ledger.add(
        LedgerSection.ASSETS,
        {"kind": "domain", "value": "example.com", "name": "New agent name"},
        engagement_id="agent-1",
    )
    before = await ledger.list()
    events = await _request(
        session,
        "save",
        entry_id=row["id"],
        expected_updated_at=row["updated_at"],
        section="targets",
        resource={"kind": "ip", "value": "192.0.2.1"},
    )
    assert "changed" in events[-1]["payload"]["detail"]
    assert await ledger.list() == before
    secret_id = await ledger.add(
        LedgerSection.CREDENTIALS,
        {"username": "user", "secret": "hidden", "host": "example.com"},
        engagement_id=session.engagement_store.engagement_id,
    )
    for entry_id in (secret_id, 100000):
        events = await _request(
            session,
            "save",
            entry_id=entry_id,
            section="assets",
            resource={"kind": "domain", "value": "example.com"},
        )
        assert "no longer exists" in events[-1]["payload"]["detail"]
        events = await _request(session, "evidence", entry_id=entry_id)
        assert "no longer exists" in events[-1]["payload"]["detail"]


@pytest.mark.asyncio
async def test_evidence_uses_selected_resource_reference_and_clips_body(
    session: WebKerkovSession,
) -> None:
    row = (await _save(session))["entries"][0]
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    evidence = await kb.add_artifact(
        engagement_id="agent-1",
        body="A" * (MAX_EVIDENCE_CHARS + 1),
        title="Selected artifact",
        source="scanner",
    )
    other = await kb.add_artifact(
        engagement_id="agent-1", body="unrelated private artifact"
    )
    await ledger.add(
        LedgerSection.ASSETS,
        row["entry"],
        engagement_id="agent-1",
        artifact_ref=evidence,
    )
    events = await _request(session, "evidence", entry_id=row["id"], artifact_ref=other)
    payload = events[-1]["payload"]
    assert events[-1]["type"] == "inventory.evidence"
    assert payload["entry_id"] == row["id"]
    assert payload["artifact"]["artifact_id"] == evidence
    assert payload["artifact"]["body"] == "A" * MAX_EVIDENCE_CHARS
    assert payload["artifact"]["truncated"] is True
    assert "unrelated private artifact" not in str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/clear", "/mode recon"])
async def test_reset_retires_inventory_before_agents_finish_and_drops_old_reads(
    session: WebKerkovSession,
    command: str,
) -> None:
    row = (await _save(session))["entries"][0]
    old_context = session.inventory.context_id
    old_kb, old_ledger = session.knowledge_base, session.findings_ledger
    assert old_kb is not None and old_ledger is not None
    read_started, release_read = asyncio.Event(), asyncio.Event()
    agents_started, release_agents = asyncio.Event(), asyncio.Event()
    original_list = old_ledger.list

    async def delayed_list(section):
        read_started.set()
        await release_read.wait()
        return await original_list(section)

    async def delayed_cancel():
        agents_started.set()
        await release_agents.wait()

    session.bus.drain_nowait()
    with (
        patch.object(old_ledger, "list", side_effect=delayed_list),
        patch.object(
            session.agent_supervisor, "cancel_all", side_effect=delayed_cancel
        ),
    ):
        read = asyncio.create_task(
            session.handle_inventory_message(
                {"type": "inventory.list", "request_id": "old-read"}
            )
        )
        await read_started.wait()
        reset = asyncio.create_task(session.handle_user_message(command))
        await agents_started.wait()
        assert session.knowledge_base is None
        assert session.findings_ledger is None
        assert session.inventory.context_id != old_context
        events = session.bus.drain_nowait()
        assert events[0]["type"] == "inventory.snapshot"
        assert events[0]["payload"]["entries"] == []
        assert events[0]["payload"]["engagement_id"] is None
        release_agents.set()
        release_read.set()
        await asyncio.wait_for(asyncio.gather(read, reset), 2)
    assert old_kb._conn is None
    assert not any(
        event["type"] == "inventory.snapshot"
        and event["payload"]["request_id"] == "old-read"
        for event in session.bus.drain_nowait()
    )
    events = await _request(
        session, "evidence", context_id=old_context, entry_id=row["id"]
    )
    assert events[-1]["type"] == "inventory.error"
    assert session.inventory.engagement_id is None


@pytest.mark.asyncio
async def test_disabled_and_failed_knowledge_base_report_unavailable(
    session: WebKerkovSession,
) -> None:
    session.config = session.config.model_copy(update={"kb_enabled": False})
    events = await _request(session, "list")
    assert events[-1]["payload"]["available"] is False
    events = await _request(
        session, "save", section="assets", resource={"kind": "ip", "value": "192.0.2.1"}
    )
    assert "KB_ENABLED" in events[-1]["payload"]["detail"]
    assert session.engagement_store.engagement_id is None
    session.config = session.config.model_copy(update={"kb_enabled": True})
    with patch(
        "kerkov.web.runtime.session.open_knowledge_base",
        side_effect=OSError("unavailable"),
    ):
        events = await _request(
            session,
            "save",
            section="assets",
            resource={"kind": "ip", "value": "192.0.2.1"},
        )
    assert events[-1]["type"] == "inventory.error"
    events = await _request(session, "list")
    assert events[-1]["payload"]["available"] is False


@pytest.mark.asyncio
async def test_unexpected_failures_do_not_expose_private_input(
    session: WebKerkovSession,
) -> None:
    await _save(session)
    with patch.object(
        session.findings_ledger,
        "save_resource",
        new=AsyncMock(side_effect=ValueError("private-secret-from-storage")),
    ):
        events = await _request(
            session,
            "save",
            section="assets",
            resource={"kind": "domain", "value": "example.com"},
        )
    assert events[-1]["type"] == "inventory.error"
    assert "private-secret" not in str(events)
