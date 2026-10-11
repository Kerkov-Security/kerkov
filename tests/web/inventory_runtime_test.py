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
    assert initial["graph"] == {"nodes": [], "edges": []}
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
async def test_snapshot_projects_connections_and_dual_roles_without_artifact_bodies(
    session: WebKerkovSession,
) -> None:
    domain = (await _save(session))["entries"][0]["id"]
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    target_domain = await ledger.add(
        LedgerSection.TARGETS,
        {"kind": "domain", "value": "example.com", "name": "Scope"},
        engagement_id="agent-1",
    )
    ip = await ledger.add(
        LedgerSection.TARGETS,
        {"kind": "ip", "value": "192.0.2.1"},
        engagement_id="agent-1",
    )
    url = await ledger.add(
        LedgerSection.TARGETS,
        {"kind": "url", "value": "https://example.com/login"},
        engagement_id="agent-1",
    )
    repository = await ledger.add(
        LedgerSection.ASSETS,
        {"kind": "repository", "value": "/srv/source"},
        engagement_id="agent-1",
    )
    evidence = await kb.add_artifact(
        engagement_id="agent-1", body="private artifact body"
    )
    observed = await ledger.add(
        LedgerSection.CONNECTIONS,
        {
            "source_id": target_domain,
            "target_id": ip,
            "relation": "resolves_to",
            "provenance": "observed",
        },
        engagement_id="agent-1",
        artifact_ref=evidence,
    )
    declared = await ledger.add(
        LedgerSection.CONNECTIONS,
        {
            "source_id": repository,
            "target_id": url,
            "relation": "deploys_to",
            "provenance": "declared",
            "note": "Deployment declared by the owner.",
        },
        engagement_id="agent-1",
    )
    await ledger.add(
        LedgerSection.CREDENTIALS,
        {"username": "private-user", "secret": "not-for-network-map"},
        engagement_id="agent-1",
    )
    with (
        patch.object(ledger, "list", wraps=ledger.list) as read,
        patch.object(
            kb, "_row_to_ledger_entry", wraps=kb._row_to_ledger_entry
        ) as decode,
        patch.object(kb, "get_artifact", wraps=kb.get_artifact) as get_artifact,
    ):
        events = await _request(session, "list")
    # A single filtered storage snapshot feeds both inventory rows and graph.
    read.assert_awaited_once_with(
        sections=(
            LedgerSection.ASSETS,
            LedgerSection.TARGETS,
            LedgerSection.CONNECTIONS,
        )
    )
    assert {call.args[0]["section"] for call in decode.call_args_list} == {
        "assets",
        "targets",
        "connections",
    }
    get_artifact.assert_not_awaited()
    payload = events[-1]["payload"]
    assert len(payload["entries"]) == 5
    assert all(row["section"] in {"assets", "targets"} for row in payload["entries"])
    nodes = {node["kind"]: node for node in payload["graph"]["nodes"]}
    assert len(nodes) == 4
    assert nodes["domain"]["roles"] == ["asset", "target"]
    assert nodes["domain"]["entry_ids"] == [domain, target_domain]
    assert nodes["domain"]["records"] == [
        {"entry_id": domain, "role": "asset", "name": "Main"},
        {"entry_id": target_domain, "role": "target", "name": "Scope"},
    ]
    edges = {edge["relation"]: edge for edge in payload["graph"]["edges"]}
    assert set(edges) == {"resolves_to", "deploys_to", "uses_host"}
    assert edges["resolves_to"]["source"] == nodes["domain"]["id"]
    assert edges["resolves_to"]["target"] == nodes["ip"]["id"]
    assert edges["resolves_to"]["provenances"] == ["observed"]
    assert edges["resolves_to"]["evidence"] == [
        {
            "connection_id": observed,
            "provenance": "observed",
            "artifact_ref": evidence,
            "note": None,
        }
    ]
    assert edges["deploys_to"]["provenances"] == ["declared"]
    assert edges["deploys_to"]["connection_ids"] == [declared]
    assert edges["deploys_to"]["source"] == nodes["repository"]["id"]
    assert edges["deploys_to"]["target"] == nodes["url"]["id"]
    assert edges["uses_host"]["source"] == nodes["url"]["id"]
    assert edges["uses_host"]["target"] == nodes["domain"]["id"]
    assert edges["uses_host"]["provenances"] == ["derived"]
    assert edges["uses_host"]["evidence"] == []
    assert "private artifact body" not in str(events)
    assert "not-for-network-map" not in str(events)
    # Connection evidence references do not widen the resource-only evidence API.
    events = await _request(session, "evidence", entry_id=observed)
    assert "no longer exists" in events[-1]["payload"]["detail"]


@pytest.mark.asyncio
async def test_reachability_snapshot_keeps_origins_and_services_without_inference(
    session: WebKerkovSession,
) -> None:
    target = (await _save(session))["entries"][0]["id"]
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    evidence = await kb.add_artifact(
        engagement_id="scanner", body="private connection response body"
    )
    observed_at = "2026-10-05T16:30:00+00:00"
    for origin, protocol, port in (
        ("local-container", "tcp", 443),
        ("remote-runner", "tcp", 443),
        ("local-container", "udp", 443),
    ):
        await ledger.add(
            LedgerSection.CONNECTIONS,
            {
                "source_vantage": origin,
                "target_id": target,
                "relation": "reaches",
                "provenance": "observed",
                "protocol": protocol,
                "port": port,
                "observed_at": observed_at,
            },
            engagement_id="scanner",
            artifact_ref=evidence,
        )
    payload = (await _request(session, "list"))[-1]["payload"]
    assert [entry["id"] for entry in payload["entries"]] == [target]
    graph = payload["graph"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    origins = [node for node in graph["nodes"] if node["kind"] == "vantage"]
    assert {node["value"] for node in origins} == {"local-container", "remote-runner"}
    assert all(
        node["entry_ids"] == node["records"] == node["roles"] == [] for node in origins
    )
    assert {
        (
            by_id[edge["source"]]["value"],
            by_id[edge["target"]]["value"],
            edge["protocol"],
            edge["port"],
        )
        for edge in graph["edges"]
    } == {
        ("local-container", "example.com", "tcp", 443),
        ("remote-runner", "example.com", "tcp", 443),
        ("local-container", "example.com", "udp", 443),
    }
    for edge in graph["edges"]:
        assert edge["relation"] == "reaches"
        assert edge["observed_at"] == observed_at
        assert edge["provenances"] == ["observed"]
        assert edge["evidence"][0]["artifact_ref"] == evidence
    assert "private connection response body" not in str(payload)

    # Removing evidence withdraws its routes and their otherwise-unused origins.
    assert kb.delete_engagement_rows("scanner") == 1
    refreshed = (await _request(session, "list"))[-1]["payload"]
    assert refreshed["graph"]["edges"] == []
    assert [node["kind"] for node in refreshed["graph"]["nodes"]] == ["domain"]
    assert refreshed["entries"] == payload["entries"]


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
@pytest.mark.parametrize("section", ["assets", "targets"])
@pytest.mark.parametrize(
    "name_fields,expected_name",
    [({}, "Agent name"), ({"name": None}, "Agent name"), ({"name": "New"}, "New")],
)
async def test_edit_into_duplicate_merges_destination_fields_and_evidence(
    session: WebKerkovSession,
    section: str,
    name_fields: dict,
    expected_name: str,
) -> None:
    original = (await _save(session))["entries"][0]
    ledger, kb = session.findings_ledger, session.knowledge_base
    assert ledger is not None and kb is not None
    evidence = await kb.add_artifact(
        engagement_id="agent-1", body="Destination evidence"
    )
    destination_id = await ledger.add(
        LedgerSection(section),
        {"kind": "domain", "value": "other.example.com", "name": "Old name"},
        engagement_id="agent-1",
        artifact_ref=evidence,
    )
    await ledger.add(
        LedgerSection(section),
        {"kind": "domain", "value": "other.example.com", "name": "Agent name"},
        engagement_id="agent-1",
    )
    snapshot = await _save(
        session,
        entry_id=original["id"],
        expected_updated_at=original["updated_at"],
        section=section,
        resource={"kind": "domain", "value": "other.example.com", **name_fields},
    )
    assert len(snapshot["entries"]) == 1
    result = snapshot["entries"][0]
    assert result["id"] == destination_id
    assert result["artifact_ref"] == evidence
    assert result["entry"]["name"] == expected_name


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["assets", "targets"])
@pytest.mark.parametrize("name_fields", [{}, {"name": None}])
async def test_same_row_edit_can_clear_name(
    session: WebKerkovSession, section: str, name_fields: dict
) -> None:
    original = (await _save(session, section=section))["entries"][0]
    snapshot = await _save(
        session,
        entry_id=original["id"],
        expected_updated_at=original["updated_at"],
        section=section,
        resource={"kind": "domain", "value": "example.com", **name_fields},
    )
    assert len(snapshot["entries"]) == 1
    result = snapshot["entries"][0]
    assert result["id"] == original["id"]
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

    async def delayed_list(*args, **kwargs):
        read_started.set()
        await release_read.wait()
        return await original_list(*args, **kwargs)

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
        assert events[0]["payload"]["graph"] == {"nodes": [], "edges": []}
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
    assert events[-1]["payload"]["graph"] == {"nodes": [], "edges": []}
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
    assert events[-1]["payload"]["graph"] == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_failed_graph_read_publishes_empty_unavailable_snapshot(
    session: WebKerkovSession,
) -> None:
    previous = await _save(session)
    assert previous["graph"]["nodes"]
    with patch.object(
        session.findings_ledger,
        "list",
        side_effect=OSError("private database path"),
    ):
        events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert payload["available"] is False
    assert payload["entries"] == []
    assert payload["graph"] == {"nodes": [], "edges": []}
    assert "private database path" not in str(events)


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
