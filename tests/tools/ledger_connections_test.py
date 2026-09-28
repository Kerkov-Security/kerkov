"""The ledger tool exposes connection facts with actionable validation errors."""

import asyncio

import pytest

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.tools.ledger_tool import LedgerTool


@pytest.fixture
def setup(tmp_path):
    kb = EngagementKnowledgeBase("root", tmp_path)
    ledger = FindingsLedger(kb)
    tool = LedgerTool(lambda: ledger, lambda: "agent")
    source = asyncio.run(
        ledger.add(
            LedgerSection.ASSETS,
            {"kind": "domain", "value": "api.example.com"},
            engagement_id="agent",
        )
    )
    target = asyncio.run(
        ledger.add(
            LedgerSection.TARGETS,
            {"kind": "ip", "value": "192.0.2.10"},
            engagement_id="agent",
        )
    )
    yield ledger, tool, {
        "source_id": source,
        "target_id": target,
        "relation": "resolves_to",
        "provenance": "declared",
        "note": "Declared by the user.",
    }
    kb.close()


def execute(tool, **params):
    return asyncio.run(tool.execute(params))


def test_tool_adds_lists_and_retracts_connections(setup):
    ledger, tool, entry = setup
    added = execute(tool, op="add", section="connections", entry=entry)
    assert added.success
    connection_id = added.metadata["entry_id"]
    listed = execute(tool, op="list", section="connections")
    assert listed.success
    assert listed.metadata["count"] == 1
    assert "resolves_to" in listed.output
    assert f"#{connection_id}" in listed.output
    assert execute(tool, op="remove", entry_id=connection_id).success
    assert execute(tool, op="list", section="connections").metadata["count"] == 0
    assert len(asyncio.run(ledger.list())) == 2


def test_tool_records_observed_connection_with_evidence(setup):
    ledger, tool, entry = setup
    artifact_ref = asyncio.run(
        ledger._kb.add_artifact(engagement_id="agent", body="DNS observation")
    )
    added = execute(
        tool,
        op="add",
        section="connections",
        entry={**entry, "provenance": "observed"},
        artifact_ref=artifact_ref,
    )
    assert added.success
    listed = execute(tool, op="list", section="connections")
    assert f"artifact#{artifact_ref}" in listed.output
    assert (
        asyncio.run(ledger.list(LedgerSection.CONNECTIONS))[0].engagement_id == "root"
    )


@pytest.mark.parametrize(
    "changes,artifact_ref,error_fragment",
    [
        ({"source_id": True}, None, "source"),
        ({"target_id": -1}, None, "target"),
        ({"target_id": 99999}, None, "endpoint"),
        ({"relation": "deploys_to"}, None, "deploys_to"),
        ({"relation": "related_to"}, None, "relation"),
        ({"provenance": "inferred"}, None, "provenance"),
        ({"note": " "}, None, "note"),
        ({"provenance": "observed"}, None, "artifact"),
        ({"provenance": "observed"}, 99999, "artifact"),
    ],
)
def test_invalid_connections_return_controlled_errors(
    setup, changes, artifact_ref, error_fragment
):
    ledger, tool, entry = setup
    before = asyncio.run(ledger.list())
    result = execute(
        tool,
        op="add",
        section="connections",
        entry={**entry, **changes},
        artifact_ref=artifact_ref,
    )
    assert not result.success
    assert error_fragment in result.error.lower()
    assert "Traceback" not in result.error
    assert asyncio.run(ledger.list()) == before


def test_removed_endpoint_returns_error_without_recreating_it(setup):
    ledger, tool, entry = setup
    assert execute(tool, op="remove", entry_id=entry["target_id"]).success
    result = execute(tool, op="add", section="connections", entry=entry)
    assert not result.success
    assert len(asyncio.run(ledger.list())) == 1
