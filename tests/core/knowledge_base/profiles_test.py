"""Profiles join exact identities without leaking unrelated ledger sections."""

import json

import pytest

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection
from kerkov.core.knowledge_base.profiles import (
    MAX_PROFILE_ROWS,
    project_resource_profile,
)


def row(entry_id, section, entry, artifact_ref=None):
    return LedgerEntry(
        id=entry_id,
        engagement_id="root",
        section=section,
        entry=entry,
        artifact_ref=artifact_ref,
        created_at="2026-10-01T12:00:00Z",
        updated_at="2026-10-02T12:00:00Z",
    )


def test_combines_roles_and_exact_records_without_inheriting_neighbors():
    rows = [
        row(
            1,
            "assets",
            {
                "kind": "domain",
                "value": "EXAMPLE.COM",
                "name": "Owned",
                "notes": "Our scope",
            },
        ),
        row(
            2,
            "targets",
            {
                "kind": "domain",
                "value": "example.com",
                "name": "Assessment",
                "environment": "Staging",
            },
        ),
        row(3, "targets", {"kind": "ip", "value": "192.0.2.1"}),
        row(4, "hosts", {"hostname": "Example.COM.", "os": "Linux"}, 41),
        row(
            5,
            "services",
            {
                "host": "example.com",
                "port": 443,
                "service": "https",
                "protocol": "tcp",
                "password": "private-field",
            },
            42,
        ),
        row(
            6,
            "findings",
            {
                "host": "example.com",
                "title": "Weak TLS",
                "severity": "high",
                "remediation": "Upgrade TLS",
            },
            43,
        ),
        row(7, "findings", {"host": "192.0.2.1", "title": "Do not inherit"}),
        row(
            8,
            "findings",
            {"host": "sub.example.com", "title": "Do not substring match"},
        ),
        row(9, "findings", {"title": "Unassigned finding"}),
        row(
            10,
            "credentials",
            {
                "host": "example.com",
                "username": "private-user",
                "password": "private-secret",
            },
            99,
        ),
        row(
            11,
            "connections",
            {
                "source_id": 1,
                "target_id": 3,
                "relation": "resolves_to",
                "provenance": "declared",
                "note": "Configured DNS",
            },
        ),
    ]
    profile = project_resource_profile(rows, 1)
    assert profile == project_resource_profile(rows, 2)
    assert profile["resource"]["roles"] == ["asset", "target"]
    assert [record["entry"]["name"] for record in profile["records"]] == [
        "Owned",
        "Assessment",
    ]
    assert profile["hosts"][0]["entry"]["os"] == "Linux"
    assert profile["services"][0]["entry"]["port"] == "443"
    assert [finding["id"] for finding in profile["findings"]] == [6]
    assert profile["related_resources"][0]["value"] == "192.0.2.1"
    assert (
        profile["last_observed_at"] is None
    )  # A write time is not an observation time.
    assert "private" not in json.dumps(profile)
    assert project_resource_profile(rows, 10) is None


@pytest.mark.parametrize(
    "kind,value,selector,other",
    [
        (
            "url",
            "https://example.com/Case?Token=A",
            "https://EXAMPLE.com/Case?Token=A",
            "https://example.com/case?Token=A",
        ),
        ("repository", "/srv/Project", "/srv/Project", "/srv/project"),
        ("ip", "2001:db8::1", "2001:0db8::1", "2001:db8::2"),
    ],
)
def test_exact_normalization_preserves_path_case(kind, value, selector, other):
    profile = project_resource_profile(
        [
            row(1, "targets", {"kind": kind, "value": value}),
            row(2, "findings", {"title": "Match", "host": selector}),
            row(3, "findings", {"title": "Other", "host": other}),
            row(4, "findings", {"title": "Host only", "host": "example.com"}),
        ],
        1,
    )
    assert [finding["id"] for finding in profile["findings"]] == [2]


def test_profile_bounds_records_and_text_without_hiding_counts():
    rows = [row(1, "targets", {"kind": "domain", "value": "example.com"})]
    rows.extend(
        row(
            i + 2,
            "findings",
            {"title": f"Finding {i}", "host": "example.com", "description": "x" * 9000},
        )
        for i in range(MAX_PROFILE_ROWS + 2)
    )
    profile = project_resource_profile(rows, 1)
    assert profile["counts"]["findings"] == MAX_PROFILE_ROWS + 2
    assert len(profile["findings"]) == MAX_PROFILE_ROWS
    assert profile["truncated"] is True
    assert profile["findings"][0]["truncated_fields"] == ["description"]
    assert len(profile["findings"][0]["entry"]["description"]) == 8192


def test_clipped_connections_keep_all_displayed_endpoint_details():
    rows = [row(1, "assets", {"kind": "domain", "value": "example.com"})]
    for index in range(MAX_PROFILE_ROWS + 20):
        rows.append(
            row(index + 2, "targets", {"kind": "ip", "value": f"192.0.2.{index + 1}"})
        )
        rows.append(
            row(
                index + 1000,
                "connections",
                {
                    "source_id": 1,
                    "target_id": index + 2,
                    "relation": "resolves_to",
                    "provenance": "declared",
                    "note": "DNS config",
                },
            )
        )
    profile = project_resource_profile(rows, 1)
    assert profile["truncated"]
    assert profile["counts"]["connections"] == MAX_PROFILE_ROWS + 20
    endpoints = {node["id"] for node in profile["related_resources"]}
    assert len(endpoints) == MAX_PROFILE_ROWS
    assert {edge["target"] for edge in profile["connections"]} == endpoints


def test_activity_labels_identify_ip_only_hosts_and_unnamed_services():
    profile = project_resource_profile(
        [
            row(1, "assets", {"kind": "ip", "value": "192.0.2.1"}),
            row(2, "hosts", {"ip": "192.0.2.1", "os": "Linux"}),
            row(3, "services", {"host": "192.0.2.1", "port": 443}),
        ],
        1,
    )
    labels = {item["id"]: item["label"] for item in profile["activity"]}
    assert labels[2] == "192.0.2.1"
    assert labels[3] == "443"


@pytest.mark.asyncio
async def test_profile_is_shared_persistent_and_read_only(tmp_path):
    kb = EngagementKnowledgeBase("root", tmp_path)
    try:
        ledger = FindingsLedger(kb)
        entry_id = await ledger.add(
            LedgerSection.TARGETS,
            {"kind": "domain", "value": "example.com"},
            engagement_id="agent",
        )
        await ledger.add(
            LedgerSection.FINDINGS,
            {"title": "Finding", "host": "example.com", "status": "open"},
            engagement_id="agent",
        )
        revision = await kb.ledger_revision()
        before = await ledger.profile(entry_id)
        assert await kb.ledger_revision() == revision
        peer = EngagementKnowledgeBase("root", tmp_path)
        try:
            assert await FindingsLedger(peer).profile(entry_id) == before
        finally:
            peer.close()
        await ledger.remove(entry_id)
        assert await ledger.profile(entry_id) is None
    finally:
        kb.close()
