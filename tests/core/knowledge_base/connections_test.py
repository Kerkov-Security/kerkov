"""Connection facts retain their meaning through resource and evidence changes."""

import asyncio
import json

import pytest

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection


@pytest.fixture
def ledger(tmp_path):
    kb = EngagementKnowledgeBase("root", tmp_path)
    yield FindingsLedger(kb)
    kb.close()


def resource(ledger, kind, value, section=LedgerSection.ASSETS):
    return asyncio.run(
        ledger.add(section, {"kind": kind, "value": value}, engagement_id="agent")
    )


def endpoints(ledger):
    return (
        resource(ledger, "domain", "api.example.com"),
        resource(ledger, "ip", "192.0.2.10"),
    )


def evidence(ledger, owner="agent"):
    return asyncio.run(
        ledger._kb.add_artifact(
            engagement_id=owner, body="api.example.com resolves to 192.0.2.10"
        )
    )


def connection(ledger, source, target, *, artifact_ref=None, **changes):
    entry = {
        "source_id": source,
        "target_id": target,
        "relation": "resolves_to",
        "provenance": "declared",
        "note": "Declared by the engagement owner.",
        **changes,
    }
    return asyncio.run(
        ledger.add(
            LedgerSection.CONNECTIONS,
            entry,
            engagement_id="agent",
            artifact_ref=artifact_ref,
        )
    )


def connections(ledger):
    return asyncio.run(ledger.list(LedgerSection.CONNECTIONS))


def save(ledger, entry_id, kind, value, section=LedgerSection.ASSETS, **extra):
    return asyncio.run(
        ledger.save_resource(
            section, {"kind": kind, "value": value, **extra}, entry_id=entry_id
        )
    )


def test_connection_is_shared_root_owned_and_persisted(ledger, tmp_path):
    source, target = endpoints(ledger)
    entry_id = connection(ledger, source, target)
    assert connections(ledger)[0].engagement_id == "root"
    assert ledger._kb.delete_engagement_ledger_rows("agent") == 0
    peer_kb = EngagementKnowledgeBase("root", tmp_path)
    try:
        row = connections(FindingsLedger(peer_kb))[0]
        assert row.id == entry_id
        assert row.entry["source_id"] == source
        assert row.entry["target_id"] == target
    finally:
        peer_kb.close()


def test_serialized_resources_and_connections_expose_usable_ledger_ids(ledger):
    source, target = endpoints(ledger)
    artifact_ref = evidence(ledger)
    entry_id = connection(
        ledger, source, target, provenance="observed", artifact_ref=artifact_ref
    )
    block = asyncio.run(ledger.serialize())
    source_line = next(
        line for line in block.splitlines() if "domain api.example.com" in line
    )
    target_line = next(line for line in block.splitlines() if "ip 192.0.2.10" in line)
    connection_line = next(line for line in block.splitlines() if "resolves_to" in line)
    assert f"#{source}" in source_line
    assert f"#{target}" in target_line
    assert f"#{entry_id}" in connection_line
    assert f"#{source}" in connection_line
    assert f"#{target}" in connection_line
    assert "observed" in connection_line
    assert f"artifact#{artifact_ref}" in connection_line


@pytest.mark.parametrize(
    "stored_entry",
    ["{", "[]", "{}"]
    + [
        json.dumps({key: value for key, value in entry.items() if key != missing})
        for entry in [
            {
                "source_id": 1,
                "target_id": 2,
                "relation": "resolves_to",
                "provenance": "declared",
            }
        ]
        for missing in entry
    ],
)
def test_corrupted_connection_does_not_break_ledger_serialization(ledger, stored_entry):
    source, target = endpoints(ledger)
    entry_id = connection(ledger, source, target)
    conn = ledger._kb._connect()
    with conn:
        conn.execute(
            "UPDATE ledger_entries SET entry = ? WHERE id = ?", (stored_entry, entry_id)
        )
    block = asyncio.run(ledger.serialize())
    assert f"#{entry_id}:" in block
    assert "?" in block
    assert "domain api.example.com" in block
    assert block == asyncio.run(ledger.serialize())


@pytest.mark.parametrize("delete", ["endpoint", "evidence"])
def test_cached_serialization_refreshes_after_peer_cleanup(ledger, tmp_path, delete):
    source, target = endpoints(ledger)
    connection(
        ledger, source, target, provenance="observed", artifact_ref=evidence(ledger)
    )
    before = asyncio.run(ledger.serialize())
    assert "resolves_to" in before
    assert asyncio.run(ledger.serialize()) == before
    peer_kb = EngagementKnowledgeBase("root", tmp_path)
    try:
        if delete == "endpoint":
            assert asyncio.run(FindingsLedger(peer_kb).remove(target))
        else:
            assert peer_kb.delete_engagement_rows("agent") == 1
        after = asyncio.run(ledger.serialize())
        assert after != before
        assert "resolves_to" not in after
        assert "domain api.example.com" in after
        assert ("ip 192.0.2.10" in after) == (delete == "evidence")
    finally:
        peer_kb.close()


def test_resource_roles_share_a_canonical_connection_identity(ledger):
    source, target = endpoints(ledger)
    source_twin = resource(ledger, "domain", "API.EXAMPLE.COM.", LedgerSection.TARGETS)
    target_twin = resource(ledger, "ip", "192.0.2.10", LedgerSection.TARGETS)
    entry_id = connection(ledger, source_twin, target_twin)
    assert connection(ledger, source, target, note="Updated declaration.") == entry_id
    rows = connections(ledger)
    assert len(rows) == 1
    assert rows[0].entry["source_id"] == source
    assert rows[0].entry["target_id"] == target
    assert rows[0].entry["note"] == "Updated declaration."


@pytest.mark.parametrize(
    "kind,value",
    [
        ("domain", "api.example.com"),
        ("ip", "192.0.2.10"),
        ("url", "https://api.example.com/app"),
    ],
)
def test_repository_can_deploy_to_each_supported_destination(ledger, kind, value):
    source = resource(ledger, "repository", "/work/api")
    target = resource(ledger, kind, value)
    connection(ledger, source, target, relation="deploys_to")
    assert connections(ledger)[0].entry["relation"] == "deploys_to"


def test_readding_observed_connection_updates_evidence_and_replaces_metadata(ledger):
    source, target = endpoints(ledger)
    first, second = evidence(ledger), evidence(ledger)
    entry_id = connection(
        ledger, source, target, provenance="observed", artifact_ref=first
    )
    assert (
        connection(
            ledger,
            source,
            target,
            provenance="observed",
            note="Second DNS observation.",
            artifact_ref=second,
        )
        == entry_id
    )
    row = connections(ledger)[0]
    assert row.artifact_ref == second
    assert row.entry["note"] == "Second DNS observation."


def test_observation_replaces_declaration_without_reusing_its_note(ledger):
    source, target = endpoints(ledger)
    entry_id = connection(ledger, source, target, note="Owner declaration.")
    artifact_ref = evidence(ledger)
    assert (
        asyncio.run(
            ledger.add(
                LedgerSection.CONNECTIONS,
                {
                    "source_id": source,
                    "target_id": target,
                    "relation": "resolves_to",
                    "provenance": "observed",
                },
                engagement_id="agent",
                artifact_ref=artifact_ref,
            )
        )
        == entry_id
    )
    row = connections(ledger)[0]
    assert row.artifact_ref == artifact_ref
    assert not row.entry.get("note")
    before = connections(ledger)
    with pytest.raises(ValueError):
        connection(ledger, source, target, provenance="observed")
    assert connections(ledger) == before


@pytest.mark.parametrize("with_declared_evidence", [False, True])
def test_declaration_preserves_prior_observation_and_evidence(
    ledger, with_declared_evidence
):
    source, target = endpoints(ledger)
    artifact_ref = evidence(ledger)
    entry_id = connection(
        ledger,
        source,
        target,
        provenance="observed",
        artifact_ref=artifact_ref,
        note="DNS response.",
    )
    before = connections(ledger)
    assert (
        connection(
            ledger,
            source,
            target,
            note="Owner confirms the mapping.",
            artifact_ref=evidence(ledger) if with_declared_evidence else None,
        )
        == entry_id
    )
    assert connections(ledger) == before
    edge = asyncio.run(ledger.graph())["edges"][0]
    assert edge["provenances"] == ["observed"]
    assert edge["evidence"][0]["artifact_ref"] == artifact_ref


@pytest.mark.parametrize("bad_id", [True, False, 0, -1, "1", 1.5, None, 2**63])
@pytest.mark.parametrize("field", ["source_id", "target_id"])
def test_invalid_endpoint_ids_leave_ledger_unchanged(ledger, bad_id, field):
    source, target = endpoints(ledger)
    before = asyncio.run(ledger.list())
    with pytest.raises(ValueError):
        connection(ledger, source, target, **{field: bad_id})
    assert asyncio.run(ledger.list()) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"relation": "related_to"},
        {"provenance": "inferred"},
        {"note": ""},
        {"note": "  "},
        {"note": None},
        {"unexpected": "field"},
    ],
)
def test_invalid_connection_schema_is_not_persisted(ledger, changes):
    source, target = endpoints(ledger)
    with pytest.raises(ValueError):
        connection(ledger, source, target, **changes)
    assert connections(ledger) == []


def test_missing_nonresource_reversed_and_self_endpoints_are_rejected(ledger):
    source, target = endpoints(ledger)
    host_id = asyncio.run(
        ledger.add(LedgerSection.HOSTS, {"ip": "192.0.2.10"}, engagement_id="agent")
    )
    for first, second in [
        (source, 99999),
        (source, host_id),
        (target, source),
        (source, source),
    ]:
        with pytest.raises(ValueError):
            connection(ledger, first, second)
    with pytest.raises(ValueError):
        connection(ledger, source, target, relation="deploys_to")
    assert connections(ledger) == []


@pytest.mark.parametrize("artifact_ref", [None, 0, -1, True, "1", 99999])
def test_observed_connections_require_an_existing_artifact(ledger, artifact_ref):
    source, target = endpoints(ledger)
    evidence(ledger)
    with pytest.raises(ValueError):
        connection(
            ledger, source, target, provenance="observed", artifact_ref=artifact_ref
        )
    assert connections(ledger) == []


def test_generic_kb_writes_cannot_bypass_connection_validation(ledger):
    source, target = endpoints(ledger)
    entry = {
        "source_id": source,
        "target_id": target,
        "relation": "resolves_to",
        "provenance": "declared",
        "note": "Owner declaration.",
    }
    for changes in [{"target_id": 99999}, {"relation": "deploys_to"}]:
        with pytest.raises(ValueError):
            asyncio.run(
                ledger._kb.add_ledger_entry(
                    engagement_id="agent",
                    section=LedgerSection.CONNECTIONS,
                    entry={**entry, **changes},
                    dedup_key="untrusted supplied identity",
                )
            )
    entry_id = asyncio.run(
        ledger._kb.add_ledger_entry(
            engagement_id="agent",
            section=LedgerSection.CONNECTIONS,
            entry=entry,
            dedup_key="untrusted supplied identity",
        )
    )
    assert connection(ledger, source, target) == entry_id
    assert connections(ledger)[0].engagement_id == "root"


def test_generic_connection_write_uses_root_owner_and_canonical_identity(ledger):
    source, target = endpoints(ledger)
    first_id = connection(ledger, source, target)
    first = connections(ledger)[0]
    other_target = resource(ledger, "ip", "192.0.2.11")
    second_id = asyncio.run(
        ledger._kb.add_ledger_entry(
            engagement_id="other-agent",
            section=LedgerSection.CONNECTIONS,
            entry={**first.entry, "target_id": other_target},
            dedup_key=ledger._kb._connection_key(first.entry),
            alias_keys=[ledger._kb._connection_key(first.entry)],
        )
    )
    assert second_id != first_id
    rows = {row.id: row for row in connections(ledger)}
    assert rows[first_id] == first
    assert rows[second_id].entry["target_id"] == other_target
    assert rows[second_id].engagement_id == "root"


def test_name_and_role_changes_preserve_connections(ledger):
    source, target = endpoints(ledger)
    entry_id = connection(ledger, source, target)
    assert (
        save(ledger, source, "domain", "api.example.com", name="Production API")
        == source
    )
    assert (
        save(ledger, source, "domain", "api.example.com", LedgerSection.TARGETS)
        == source
    )
    row = connections(ledger)[0]
    assert row.id == entry_id
    assert row.entry["source_id"] == source


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_identity_edit_retires_connections_instead_of_transferring_them(
    ledger, endpoint
):
    source, target = endpoints(ledger)
    connection(ledger, source, target)
    if endpoint == "source":
        save(ledger, source, "domain", "other.example.com")
    else:
        save(ledger, target, "ip", "192.0.2.11")
    assert connections(ledger) == []


def test_identity_edit_keeps_original_fact_via_remaining_role(ledger):
    source, target = endpoints(ledger)
    twin = resource(ledger, "domain", "api.example.com", LedgerSection.TARGETS)
    entry_id = connection(ledger, source, target)
    save(ledger, source, "domain", "other.example.com")
    row = connections(ledger)[0]
    assert row.id == entry_id
    assert row.entry["source_id"] == twin
    assert row.entry["target_id"] == target


def test_role_merge_redirects_connections_to_the_surviving_id(ledger):
    source, target = endpoints(ledger)
    twin = resource(ledger, "domain", "api.example.com", LedgerSection.TARGETS)
    entry_id = connection(ledger, source, target)
    assert (
        save(ledger, source, "domain", "api.example.com", LedgerSection.TARGETS) == twin
    )
    row = connections(ledger)[0]
    assert row.id == entry_id
    assert row.entry["source_id"] == twin
    assert connection(ledger, twin, target) == entry_id


@pytest.mark.parametrize("observed_destination", [False, True])
def test_role_canonicalization_preserves_observed_evidence_on_duplicate(
    ledger, observed_destination
):
    source, target = endpoints(ledger)
    twin = resource(ledger, "domain", "api.example.com", LedgerSection.TARGETS)
    artifact_ref = evidence(ledger)
    entry_id = connection(
        ledger,
        source,
        target,
        provenance="declared" if observed_destination else "observed",
        artifact_ref=None if observed_destination else artifact_ref,
    )
    # Historical rows may reference separate roles of the same resource before
    # canonicalization. Give the declaration the newer timestamp in either case.
    duplicate = {
        **connections(ledger)[0].entry,
        "source_id": twin,
        "provenance": "observed" if observed_destination else "declared",
    }
    conn = ledger._kb._connect()
    with conn:
        conn.execute(
            "UPDATE ledger_entries SET updated_at = ? WHERE id = ?",
            ("2026-02-01" if observed_destination else "2026-01-01", entry_id),
        )
        cursor = conn.execute(
            "INSERT INTO ledger_entries (engagement_id, section, entry, dedup_key, "
            "artifact_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "root",
                "connections",
                json.dumps(duplicate),
                ledger._kb._connection_key(duplicate),
                artifact_ref if observed_destination else None,
                "2026-01-01",
                "2026-01-01" if observed_destination else "2026-02-01",
            ),
        )
        destination_id = cursor.lastrowid
    assert asyncio.run(ledger.remove(source))
    rows = connections(ledger)
    assert len(rows) == 1
    assert rows[0].id == destination_id
    assert rows[0].entry["source_id"] == twin
    assert rows[0].entry["provenance"] == "observed"
    assert rows[0].artifact_ref == artifact_ref


@pytest.mark.parametrize("keep_old_role", [False, True])
def test_edit_into_an_existing_identity_preserves_each_original_fact(
    ledger, keep_old_role
):
    source, target = endpoints(ledger)
    destination = resource(ledger, "domain", "other.example.com")
    other_target = resource(ledger, "ip", "192.0.2.11")
    old_fact = connection(ledger, source, target)
    destination_fact = connection(ledger, destination, other_target)
    twin = (
        resource(ledger, "domain", "api.example.com", LedgerSection.TARGETS)
        if keep_old_role
        else None
    )
    assert save(ledger, source, "domain", "other.example.com") == destination
    rows = {row.id: row for row in connections(ledger)}
    assert rows[destination_fact].entry["source_id"] == destination
    assert rows[destination_fact].entry["target_id"] == other_target
    if keep_old_role:
        assert set(rows) == {old_fact, destination_fact}
        assert rows[old_fact].entry["source_id"] == twin
        assert rows[old_fact].entry["target_id"] == target
    else:
        assert set(rows) == {destination_fact}


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_older_row_becoming_role_twin_recanonicalizes_existing_connections(
    ledger, endpoint
):
    kind, old_value, new_value = (
        ("domain", "old.example.com", "api.example.com")
        if endpoint == "source"
        else ("ip", "192.0.2.9", "192.0.2.10")
    )
    older = resource(ledger, kind, old_value)
    newer = resource(ledger, kind, new_value)
    other = (
        resource(ledger, "ip", "192.0.2.10")
        if endpoint == "source"
        else resource(ledger, "domain", "api.example.com")
    )
    source, target = (newer, other) if endpoint == "source" else (other, newer)
    entry_id = connection(ledger, source, target)
    assert save(ledger, older, kind, new_value, LedgerSection.TARGETS) == older
    row = connections(ledger)[0]
    assert row.id == entry_id
    assert row.entry[f"{endpoint}_id"] == older
    for resource_id in (older, newer):
        source, target = (
            (resource_id, other) if endpoint == "source" else (other, resource_id)
        )
        assert connection(ledger, source, target) == entry_id
    assert len(connections(ledger)) == 1


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_remove_one_role_rehomes_and_remove_last_role_cascades(ledger, endpoint):
    source, target = endpoints(ledger)
    removed_id = source if endpoint == "source" else target
    kind, value = (
        ("domain", "api.example.com") if endpoint == "source" else ("ip", "192.0.2.10")
    )
    twin = resource(ledger, kind, value, LedgerSection.TARGETS)
    entry_id = connection(ledger, source, target)
    assert asyncio.run(ledger.remove(removed_id))
    row = connections(ledger)[0]
    assert row.id == entry_id
    assert row.entry[f"{endpoint}_id"] == twin
    assert asyncio.run(ledger.remove(twin))
    assert connections(ledger) == []


def test_deleting_evidence_retracts_only_its_observations(ledger):
    source, target = endpoints(ledger)
    observed_id = connection(
        ledger, source, target, provenance="observed", artifact_ref=evidence(ledger)
    )
    second_target = resource(ledger, "ip", "192.0.2.11")
    declared_id = connection(
        ledger, source, second_target, artifact_ref=evidence(ledger)
    )
    third_target = resource(ledger, "ip", "192.0.2.12")
    retained_id = connection(
        ledger,
        source,
        third_target,
        provenance="observed",
        artifact_ref=evidence(ledger, owner="other-agent"),
    )
    assert ledger._kb.delete_engagement_rows("agent") == 2
    rows = {row.id: row for row in connections(ledger)}
    assert observed_id not in rows
    assert set(rows) == {declared_id, retained_id}
    assert rows[declared_id].artifact_ref is None
    assert rows[retained_id].artifact_ref is not None


@pytest.mark.parametrize("delete", ["endpoint", "evidence"])
def test_competing_writes_do_not_leave_dangling_connections(ledger, tmp_path, delete):
    source, target = endpoints(ledger)
    artifact_ref = evidence(ledger)
    peer_kb = EngagementKnowledgeBase("root", tmp_path)
    peer = FindingsLedger(peer_kb)

    async def race():
        deleting = (
            peer.remove(target)
            if delete == "endpoint"
            else asyncio.to_thread(peer_kb.delete_engagement_rows, "agent")
        )
        return await asyncio.gather(
            ledger.add(
                LedgerSection.CONNECTIONS,
                {
                    "source_id": source,
                    "target_id": target,
                    "relation": "resolves_to",
                    "provenance": "observed",
                },
                engagement_id="agent",
                artifact_ref=artifact_ref,
            ),
            deleting,
            return_exceptions=True,
        )

    try:
        results = asyncio.run(race())
        assert all(
            not isinstance(result, Exception) or isinstance(result, ValueError)
            for result in results
        )
        assert not isinstance(results[1], Exception)
        assert connections(ledger) == []
    finally:
        peer_kb.close()
