"""Direct reachability retains source, service identity, and observation evidence."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import Connection, project_resource_graph
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection


def route(**changes):
    return {
        "source_vantage": "runner:assessment-a",
        "target_id": 1,
        "relation": "reaches",
        "provenance": "declared",
        "protocol": "tcp",
        "port": 443,
        "note": "The supplied configuration permits this direct connection.",
        **changes,
    }


@pytest.fixture
def ledger(tmp_path):
    kb = EngagementKnowledgeBase("root", tmp_path)
    yield FindingsLedger(kb)
    kb.close()


def resource(ledger, kind, value, section=LedgerSection.ASSETS):
    return asyncio.run(
        ledger.add(section, {"kind": kind, "value": value}, engagement_id="agent")
    )


def write(ledger, entry, artifact_ref=None):
    return asyncio.run(ledger.add_connection(entry, artifact_ref=artifact_ref))


def stored(ledger):
    return asyncio.run(ledger.list(LedgerSection.CONNECTIONS))


def artifact(ledger, owner="agent"):
    return asyncio.run(
        ledger._kb.add_artifact(engagement_id=owner, body="TCP handshake")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_vantage": None},
        {"source_id": 2},
        {"source_vantage": " "},
        {"source_vantage": "10.10.14.5"},
        {"source_vantage": "2001:db8::1"},
        {"source_vantage": "a" * 201},
        {"source_vantage": "runner\nother"},
        {"protocol": None},
        {"protocol": "https"},
        {"protocol": "TCP"},
        {"port": None},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"port": "443"},
        {"port": 443.0},
        {"service": " "},
        {"service": "a" * 81},
        {"service": "https\tssh"},
        {"observed_at": "2026-10-05T12:00:00Z"},
        {"target_id": True},
    ],
)
def test_reachability_rejects_ambiguous_or_invalid_fields(changes):
    with pytest.raises(ValidationError):
        Connection.model_validate(route(**changes))


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-05",
        "2026-10-05T12:00:00",
        "2026-10-05 12:00:00Z",
        "2026-02-30T12:00:00Z",
        "2026-10-05T12:00:00+99:00",
        "2026-10-05T12:00:00+01:99",
        1234,
        True,
    ],
)
def test_observation_time_requires_a_real_timezone_aware_timestamp(timestamp):
    with pytest.raises(ValidationError):
        Connection.model_validate(route(provenance="observed", observed_at=timestamp))


@pytest.mark.parametrize("relation", ["resolves_to", "deploys_to"])
@pytest.mark.parametrize(
    "field", ["source_vantage", "protocol", "port", "service", "observed_at"]
)
def test_legacy_relations_reject_route_fields_even_null(relation, field):
    with pytest.raises(ValidationError, match="only allowed for reaches"):
        Connection.model_validate(
            {
                "source_id": 1,
                "target_id": 2,
                "relation": relation,
                "provenance": "declared",
                "note": "Explicit declaration",
                field: None,
            }
        )


def test_observation_time_normalizes_to_utc_and_labels_trim():
    connection = Connection.model_validate(
        route(
            source_vantage=" runner:assessment-a ",
            service=" https ",
            provenance="observed",
            observed_at="2026-10-05T08:15:30.123456-04:00",
        )
    )
    assert connection.observed_at == "2026-10-05T12:15:30.123456+00:00"
    assert connection.service == "https"
    assert connection.source_vantage == "runner:assessment-a"


@pytest.mark.parametrize(
    "kind,value", [("url", "https://api.example/"), ("repository", "/work/repo")]
)
@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_route_endpoints_must_be_existing_host_resources(ledger, kind, value, endpoint):
    host = resource(ledger, "domain", "api.example")
    invalid = resource(ledger, kind, value)
    entry = route(target_id=invalid if endpoint == "target" else host)
    if endpoint == "source":
        entry.pop("source_vantage")
        entry["source_id"] = invalid
    with pytest.raises(ValueError, match="domain or IP"):
        write(ledger, entry)
    assert stored(ledger) == []


def test_route_requires_existing_evidence_and_endpoints(ledger):
    target = resource(ledger, "ip", "192.0.2.1")
    entry = route(
        target_id=target, provenance="observed", observed_at="2026-10-05T12:00:00Z"
    )
    for evidence in (None, 999):
        with pytest.raises(ValueError, match="artifact"):
            write(ledger, entry, evidence)
    with pytest.raises(ValueError, match="existing"):
        write(ledger, route(target_id=999))
    assert stored(ledger) == []


def test_ports_transports_sources_and_directions_are_distinct_and_persist(
    ledger, tmp_path
):
    source = resource(ledger, "domain", "runner.example")
    target = resource(ledger, "ip", "192.0.2.1")
    entries = [
        route(target_id=target),
        route(target_id=target, port=8443),
        route(target_id=target, protocol="udp"),
        route(target_id=target, source_vantage="runner:assessment-b"),
        route(target_id=target, source_vantage=None, source_id=source),
        route(target_id=source, source_vantage=None, source_id=target),
    ]
    ids = [write(ledger, entry).entry_id for entry in entries]
    assert len(set(ids)) == len(entries)
    updated = write(
        ledger, {**entries[0], "service": "https", "note": "Updated config"}
    )
    assert updated.entry_id == ids[0]
    assert len(stored(ledger)) == len(entries)
    peer = EngagementKnowledgeBase("root", tmp_path)
    try:
        assert asyncio.run(FindingsLedger(peer).graph()) == asyncio.run(ledger.graph())
        assert all(row.engagement_id == "root" for row in stored(FindingsLedger(peer)))
    finally:
        peer.close()


def test_observed_routes_keep_fresh_evidence_against_late_older_writes(ledger):
    target = resource(ledger, "domain", "api.example")
    declared = route(target_id=target)
    original = write(ledger, declared)
    old_ref, fresh_ref = artifact(ledger), artifact(ledger)
    fresh = {
        **declared,
        "provenance": "observed",
        "observed_at": "2026-10-05T12:00:00Z",
        "service": "https",
    }
    assert write(ledger, fresh, fresh_ref).entry_id == original.entry_id
    before = stored(ledger)
    # Comparison uses actual instants, not lexicographic timezone strings.
    old = {**fresh, "observed_at": "2026-10-05T13:00:00+02:00", "service": "old-label"}
    assert write(ledger, old, old_ref).outcome == "stale_observation_retained"
    assert write(ledger, declared).outcome == "observation_retained"
    assert stored(ledger) == before
    fresh["observed_at"] = "2026-10-05T12:00:01Z"
    fresh.pop("service")
    assert write(ledger, fresh, old_ref).outcome == "recorded"
    [saved] = stored(ledger)
    assert saved.artifact_ref == old_ref
    assert "service" not in saved.entry


@pytest.mark.parametrize("vantage", [True, False])
def test_routes_follow_surviving_roles_then_retract_with_endpoint(ledger, vantage):
    source = resource(ledger, "domain", "runner.example")
    source_twin = resource(ledger, "domain", "runner.example", LedgerSection.TARGETS)
    target = resource(ledger, "domain", "api.example")
    target_twin = resource(ledger, "domain", "api.example", LedgerSection.TARGETS)
    entry = route(target_id=target_twin)
    if not vantage:
        entry.pop("source_vantage")
        entry["source_id"] = source_twin
    added = write(ledger, entry)
    assert stored(ledger)[0].entry["target_id"] == target
    if not vantage:
        assert stored(ledger)[0].entry["source_id"] == source
    assert asyncio.run(ledger.remove(source))
    assert asyncio.run(ledger.remove(target))
    assert stored(ledger)[0].entry["target_id"] == target_twin
    if not vantage:
        assert stored(ledger)[0].entry["source_id"] == source_twin
    assert write(ledger, entry).entry_id == added.entry_id
    assert asyncio.run(ledger.remove(target_twin))
    assert stored(ledger) == []
    assert not any(
        node["kind"] == "vantage" for node in asyncio.run(ledger.graph())["nodes"]
    )


def test_editing_source_identity_retracts_the_original_route(ledger):
    source = resource(ledger, "domain", "runner.example")
    target = resource(ledger, "domain", "api.example")
    write(ledger, route(source_vantage=None, source_id=source, target_id=target))
    asyncio.run(
        ledger.save_resource(
            LedgerSection.ASSETS,
            {"kind": "domain", "value": "other.example"},
            entry_id=source,
        )
    )
    assert stored(ledger) == []


def test_evidence_cleanup_removes_observed_route_but_keeps_configuration(ledger):
    target = resource(ledger, "ip", "192.0.2.1")
    ref = artifact(ledger)
    write(
        ledger,
        route(
            target_id=target, provenance="observed", observed_at="2026-10-05T12:00:00Z"
        ),
        ref,
    )
    declaration = write(ledger, route(target_id=target, port=22), ref)
    ledger._kb.delete_engagement_rows("agent")
    [saved] = stored(ledger)
    assert saved.id == declaration.entry_id
    assert saved.artifact_ref is None
    assert "observed_at" not in saved.entry


def test_graph_projects_stable_virtual_sources_and_preserves_observation_metadata(
    ledger,
):
    target = resource(ledger, "domain", "api.example")
    write(
        ledger,
        route(
            target_id=target,
            provenance="observed",
            service="https",
            observed_at="2026-10-05T12:00:00Z",
        ),
        artifact(ledger),
    )
    graph = asyncio.run(ledger.graph())
    [vantage] = [node for node in graph["nodes"] if node["kind"] == "vantage"]
    assert vantage["roles"] == vantage["entry_ids"] == vantage["records"] == []
    assert vantage["value"] == "runner:assessment-a"
    assert vantage["id"].startswith("vantage:")
    [edge] = graph["edges"]
    assert edge["source"] == vantage["id"]
    assert (edge["protocol"], edge["port"], edge["service"]) == ("tcp", 443, "https")
    assert (
        edge["observed_at"]
        == edge["evidence"][0]["observed_at"]
        == "2026-10-05T12:00:00+00:00"
    )
    assert edge["evidence"][0]["service"] == "https"
    assert "TCP handshake" not in json.dumps(graph)
    block = asyncio.run(ledger.serialize())
    assert 'vantage "runner:assessment-a"' in block
    assert "tcp:443" in block
    assert "observed_at=2026-10-05T12:00:00+00:00" in block


def test_projection_keeps_freshest_route_from_historical_role_duplicates(ledger):
    target = resource(ledger, "domain", "api.example")
    write(
        ledger,
        route(
            target_id=target,
            provenance="observed",
            service="https",
            observed_at="2026-10-05T12:00:00Z",
        ),
        artifact(ledger),
    )
    rows = asyncio.run(ledger.list())
    observation = stored(ledger)[0]
    duplicate = LedgerEntry(
        **{
            **vars(observation),
            "id": observation.id + 1,
            "entry": {
                **observation.entry,
                "observed_at": "2026-10-05T11:00:00+00:00",
                "service": "stale",
            },
        }
    )
    graph = project_resource_graph([*rows, duplicate])
    [edge] = graph["edges"]
    assert edge["service"] == "https"
    assert len(edge["evidence"]) == 2
    assert graph == project_resource_graph([duplicate, *reversed(rows)])


@pytest.mark.parametrize("destination_freshest", [True, False])
def test_role_collision_prefers_observation_time_over_write_time(
    ledger, destination_freshest
):
    target = resource(ledger, "domain", "api.example")
    twin = resource(ledger, "domain", "api.example", LedgerSection.TARGETS)
    fresh_ref, old_ref = artifact(ledger), artifact(ledger)
    fresh_time = "2026-10-05T12:00:00+00:00"
    old_time = "2026-10-05T11:00:00+00:00"
    entry = route(
        target_id=target,
        provenance="observed",
        observed_at=old_time if destination_freshest else fresh_time,
    )
    added = write(ledger, entry, old_ref if destination_freshest else fresh_ref)
    duplicate = {
        **entry,
        "target_id": twin,
        "observed_at": fresh_time if destination_freshest else old_time,
    }
    conn = ledger._kb._connect()
    with conn:
        conn.execute(
            "UPDATE ledger_entries SET updated_at = ? WHERE id = ?",
            ("2026-10-06" if destination_freshest else "2026-10-05", added.entry_id),
        )
        cursor = conn.execute(
            "INSERT INTO ledger_entries (engagement_id, section, entry, dedup_key, "
            "artifact_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "root",
                "connections",
                json.dumps(duplicate),
                ledger._kb._connection_key(duplicate),
                fresh_ref if destination_freshest else old_ref,
                "2026-10-05",
                "2026-10-05" if destination_freshest else "2026-10-06",
            ),
        )
        destination_id = cursor.lastrowid
    assert asyncio.run(ledger.remove(target))
    [saved] = stored(ledger)
    assert saved.id == destination_id
    assert saved.entry["target_id"] == twin
    assert saved.entry["observed_at"] == fresh_time
    assert saved.artifact_ref == fresh_ref


def test_invalid_historical_observation_does_not_create_orphan_vantage(ledger):
    target = resource(ledger, "domain", "api.example")
    write(ledger, route(target_id=target))
    rows = asyncio.run(ledger.list())
    [connection] = [row for row in rows if row.section is LedgerSection.CONNECTIONS]
    connection.entry["provenance"] = "observed"
    connection.artifact_ref = 1
    graph = project_resource_graph(rows)
    assert graph["edges"] == []
    assert [node["kind"] for node in graph["nodes"]] == ["domain"]


def test_observation_defaults_to_evidence_time_and_rejects_future(ledger):
    target = resource(ledger, "domain", "api.example")
    ref = artifact(ledger)
    entry = route(target_id=target, provenance="observed")
    write(ledger, entry, ref)
    before = stored(ledger)
    evidence_time = (
        ledger._kb._connect()
        .execute("SELECT created_at FROM artifacts WHERE id = ?", (ref,))
        .fetchone()["created_at"]
    )
    assert before[0].entry["observed_at"] == evidence_time
    with pytest.raises(ValueError, match="later than the evidence"):
        write(ledger, {**entry, "observed_at": "2099-01-01T00:00:00Z"}, ref)
    assert stored(ledger) == before
    later_ref = artifact(ledger)
    assert write(ledger, entry, later_ref).outcome == "recorded"
    assert stored(ledger)[0].artifact_ref == later_ref


@pytest.mark.parametrize("vantage", ["runner.example", "RUNNER.EXAMPLE."])
def test_vantage_cannot_duplicate_existing_host(ledger, vantage):
    resource(ledger, "domain", "runner.example")
    target = resource(ledger, "domain", "api.example")
    with pytest.raises(ValueError, match="use source_id"):
        write(ledger, route(target_id=target, source_vantage=vantage))
    assert stored(ledger) == []


def test_vantage_route_absorbs_when_matching_host_added_later(ledger):
    target = resource(ledger, "domain", "api.example")
    write(ledger, route(source_vantage="runner.example", target_id=target))
    source = resource(ledger, "domain", "runner.example")
    rows = stored(ledger)
    assert len(rows) == 1
    assert rows[0].entry.get("source_vantage") is None
    assert rows[0].entry["source_id"] == source
    graph = asyncio.run(ledger.graph())
    assert all(node["kind"] != "vantage" for node in graph["nodes"])


def test_vantage_route_absorbs_through_web_resource_save(ledger):
    target = resource(ledger, "domain", "api.example")
    write(ledger, route(source_vantage="RUNNER.EXAMPLE.", target_id=target))
    source = asyncio.run(
        ledger.save_resource(
            LedgerSection.ASSETS, {"kind": "domain", "value": "runner.example"}
        )
    )
    rows = stored(ledger)
    assert len(rows) == 1
    assert rows[0].entry.get("source_vantage") is None
    assert rows[0].entry["source_id"] == source


def test_editing_host_value_absorbs_matching_vantage_route(ledger):
    target = resource(ledger, "domain", "api.example")
    host = resource(ledger, "domain", "other.example")
    write(ledger, route(source_vantage="runner.example", target_id=target))
    asyncio.run(
        ledger.save_resource(
            LedgerSection.ASSETS,
            {"kind": "domain", "value": "runner.example"},
            entry_id=host,
        )
    )
    rows = stored(ledger)
    assert len(rows) == 1
    assert rows[0].entry["source_id"] == host


def test_non_matching_host_leaves_vantage_route_untouched(ledger):
    target = resource(ledger, "domain", "api.example")
    write(ledger, route(source_vantage="runner.example", target_id=target))
    resource(ledger, "domain", "unrelated.example")
    rows = stored(ledger)
    assert len(rows) == 1
    assert rows[0].entry["source_vantage"] == "runner.example"
