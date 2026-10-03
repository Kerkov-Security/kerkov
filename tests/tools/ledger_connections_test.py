"""The ledger tool exposes connection facts with actionable validation errors."""

import asyncio
import json

import google.generativeai as genai
import pytest

from kerkov.core.knowledge_base.connections import SQLITE_MAX_ID
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.core.tool_output import TOOL_OUTPUT_MAX_CHARS, normalize_tool_output
from kerkov.providers.gemini_provider import _normalize_proto_value
from kerkov.tools.ledger_tool import GRAPH_PAGE_MAX_CHARS, LedgerTool, LedgerToolParams


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


@pytest.mark.parametrize("with_evidence", [False, True])
def test_tool_reports_when_declaration_is_not_stored(setup, with_evidence):
    ledger, tool, entry = setup
    artifact_ref = asyncio.run(
        ledger._kb.add_artifact(engagement_id="agent", body="DNS observation")
    )
    observed = execute(
        tool,
        op="add",
        section="connections",
        entry={**entry, "provenance": "observed", "note": "DNS response."},
        artifact_ref=artifact_ref,
    )
    twin = execute(
        tool,
        op="add",
        section="targets",
        entry={"kind": "domain", "value": "api.example.com"},
    ).metadata["entry_id"]
    before = asyncio.run(ledger.list())
    declared = execute(
        tool,
        op="add",
        section="connections",
        entry={**entry, "source_id": twin},
        artifact_ref=artifact_ref if with_evidence else None,
    )
    assert declared.success
    assert declared.metadata == {**observed.metadata, "outcome": "observation_retained"}
    assert "Kept observed connection" in declared.output
    assert "declaration and its note were not stored" in declared.output
    assert asyncio.run(ledger.list()) == before


def test_gemini_function_call_records_integral_connection_ids(setup):
    ledger, tool, entry = setup
    artifact_ref = asyncio.run(
        ledger._kb.add_artifact(engagement_id="agent", body="DNS observation")
    )
    call = genai.protos.FunctionCall(
        name="ledger",
        args={
            "op": "add",
            "section": "connections",
            "entry": {**entry, "provenance": "observed"},
            "artifact_ref": artifact_ref,
        },
    )
    params = _normalize_proto_value(call.args)
    assert type(params["entry"]["source_id"]) is float
    assert type(params["entry"]["target_id"]) is float
    assert type(params["artifact_ref"]) is float
    result = execute(tool, **params)
    assert result.success, result.error
    [row] = asyncio.run(ledger.list(LedgerSection.CONNECTIONS))
    assert row.entry["source_id"] == entry["source_id"]
    assert row.entry["target_id"] == entry["target_id"]
    assert type(row.entry["source_id"]) is int
    assert type(row.entry["target_id"]) is int
    assert row.artifact_ref == artifact_ref
    # Boundary validation never mutates the provider's persisted raw arguments.
    assert type(params["entry"]["source_id"]) is float


@pytest.mark.parametrize("field", ["source_id", "target_id", "artifact_ref"])
@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "1",
        1.5,
        float("nan"),
        float("inf"),
        -float("inf"),
        0,
        -1,
        SQLITE_MAX_ID + 1,
        float(SQLITE_MAX_ID),
    ],
)
def test_connection_ids_reject_invalid_provider_values(setup, field, value):
    ledger, tool, entry = setup
    params = {"op": "add", "section": "connections", "entry": dict(entry)}
    if field == "artifact_ref":
        params[field] = value
    else:
        params["entry"][field] = value
    before = asyncio.run(ledger.list())
    result = execute(tool, **params)
    assert not result.success
    assert field in result.error
    assert asyncio.run(ledger.list()) == before


def test_integer_ids_retain_sqlite_precision():
    params = LedgerToolParams(
        op="add",
        section="connections",
        entry={"source_id": SQLITE_MAX_ID, "target_id": 1.0},
        artifact_ref=SQLITE_MAX_ID,
    )
    assert params.entry == {"source_id": SQLITE_MAX_ID, "target_id": 1}
    assert params.artifact_ref == SQLITE_MAX_ID


def test_tool_graph_keeps_small_inventory_and_all_edges_visible(setup):
    ledger, tool, first_entry = setup
    recorded_ids = []
    artifact_ref = asyncio.run(
        ledger._kb.add_artifact(engagement_id="agent", body="DNS observations")
    )

    def add_resource(kind, value):
        result = execute(
            tool, op="add", section="assets", entry={"kind": kind, "value": value}
        )
        assert result.success, result.error
        return result.metadata["entry_id"]

    for index in range(8):
        domain = "api.example.com" if index == 0 else f"api{index}.example.com"
        source_id = (
            first_entry["source_id"] if index == 0 else add_resource("domain", domain)
        )
        target_id = (
            first_entry["target_id"]
            if index == 0
            else add_resource("ip", f"192.0.2.{index + 10}")
        )
        add_resource("url", f"https://{domain}/api")
        result = execute(
            tool,
            op="add",
            section="connections",
            entry={
                "source_id": source_id,
                "target_id": target_id,
                "relation": "resolves_to",
                "provenance": "observed",
                "note": "DNS evidence. " * 100,
            },
            artifact_ref=artifact_ref,
        )
        assert result.success, result.error
        recorded_ids.append(result.metadata["entry_id"])

    full_graph = asyncio.run(ledger.graph())
    result = execute(tool, op="graph")
    assert result.success
    assert len(result.output) + len(repr(result.metadata)) < TOOL_OUTPUT_MAX_CHARS
    preview, normalized = normalize_tool_output(result.output)
    assert not normalized.was_clipped
    graph = json.loads(preview)
    assert result.metadata == {"node_count": 24, "edge_count": 16}
    assert len(graph["nodes"]) == 24
    assert len(graph["edges"]) == 16
    assert graph["next_offset"] is None
    node_ids = {node["id"]: node["entry_ids"][0] for node in full_graph["nodes"]}
    assert {
        (edge["source_id"], edge["relation"], edge["target_id"])
        for edge in graph["edges"]
    } == {
        (node_ids[edge["source"]], edge["relation"], node_ids[edge["target"]])
        for edge in full_graph["edges"]
    }
    assert sorted(
        entry_id for edge in graph["edges"] for entry_id in edge["connection_ids"]
    ) == sorted(recorded_ids)
    assert {entry_id for node in graph["nodes"] for entry_id in node["entry_ids"]} == {
        entry_id for node in full_graph["nodes"] for entry_id in node["entry_ids"]
    }
    for edge in graph["edges"]:
        if edge["relation"] == "uses_host":
            assert edge["connection_ids"] == []
            assert edge["provenances"] == ["derived"]
        else:
            assert edge["artifact_refs"] == [artifact_ref]
            assert edge["provenances"] == ["observed"]
    assert "DNS evidence" not in result.output
    assert "DNS evidence" in execute(tool, op="list", section="connections").output
    assert asyncio.run(ledger.graph()) == full_graph
    assert all(node["id"].startswith("resource:") for node in full_graph["nodes"])
    # Graph handles work directly in subsequent mutations.
    assert execute(tool, op="remove", entry_id=recorded_ids[0]).success
    assert execute(
        tool,
        op="add",
        section="connections",
        entry=first_entry,
    ).success


def test_tool_graph_retains_roles_names_and_all_resource_handles(setup):
    ledger, tool, entry = setup
    added = execute(
        tool,
        op="add",
        section="targets",
        entry={"kind": "domain", "value": "api.example.com", "name": "Audit scope"},
    )
    graph = json.loads(execute(tool, op="graph").output)
    [domain] = [node for node in graph["nodes"] if node["kind"] == "domain"]
    assert domain["roles"] == ["asset", "target"]
    assert domain["entry_ids"] == [entry["source_id"], added.metadata["entry_id"]]
    assert domain["names"] == {"target": "Audit scope"}


def test_tool_graph_without_resources_returns_empty_arrays(setup):
    ledger, tool, entry = setup
    for entry_id in (entry["source_id"], entry["target_id"]):
        assert execute(tool, op="remove", entry_id=entry_id).success
    result = execute(tool, op="graph")
    assert result.success
    assert json.loads(result.output) == {"nodes": [], "edges": [], "next_offset": None}
    assert result.metadata == {"node_count": 0, "edge_count": 0}


def test_large_graph_pages_preserve_every_resource_and_connection(setup):
    ledger, tool, entry = setup
    for index in range(60):
        url = execute(
            tool,
            op="add",
            section="targets",
            entry={"kind": "url", "value": f"https://api.example.com/{index}"},
        )
        assert url.success
    assert execute(tool, op="add", section="connections", entry=entry).success
    full = asyncio.run(ledger.graph())
    collected = {"nodes": [], "edges": []}
    offset = 0
    pages = 0
    while True:
        result = execute(tool, op="graph", offset=offset)
        assert result.success, result.error
        assert len(result.output) <= GRAPH_PAGE_MAX_CHARS
        preview, normalization = normalize_tool_output(result.output)
        assert not normalization.was_clipped
        page = json.loads(preview)
        pages += 1
        for section in collected:
            collected[section].extend(page[section])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert pages > 1
    assert len(collected["nodes"]) == len(full["nodes"])
    assert len(collected["edges"]) == len(full["edges"])
    assert [node["entry_ids"] for node in collected["nodes"]] == [
        node["entry_ids"] for node in full["nodes"]
    ]
    handles = {node["id"]: node["entry_ids"][0] for node in full["nodes"]}
    assert [
        (edge["source_id"], edge["target_id"], edge["relation"], edge["connection_ids"])
        for edge in collected["edges"]
    ] == [
        (
            handles[edge["source"]],
            handles[edge["target"]],
            edge["relation"],
            edge["connection_ids"],
        )
        for edge in full["edges"]
    ]
    assert asyncio.run(ledger.graph()) == full
    assert not execute(tool, op="graph", offset=99999).success
    assert not execute(tool, op="graph", offset=-1).success


def test_oversized_resource_text_does_not_block_graph_pagination(setup):
    ledger, tool, entry = setup
    value = "https://api.example.com/" + "a" * 6000
    added = execute(
        tool,
        op="add",
        section="assets",
        entry={"kind": "url", "value": value, "name": "界" * 6000},
    )
    assert added.success
    offset = 0
    nodes = []
    while True:
        result = execute(tool, op="graph", offset=offset)
        assert result.success, result.error
        assert len(result.output) <= GRAPH_PAGE_MAX_CHARS
        page = json.loads(result.output)
        nodes.extend(page["nodes"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    [node] = [node for node in nodes if added.metadata["entry_id"] in node["entry_ids"]]
    assert node["omitted_fields"] == ["value", "names"]
    assert "value" not in node
    assert "names" not in node
    assert value in [node["value"] for node in asyncio.run(ledger.graph())["nodes"]]


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
        ({"note": "first\nsecond"}, None, "control characters"),
        ({"note": "x" * 2001}, None, "2000"),
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


def test_self_connection_error_explains_the_problem(setup):
    ledger, tool, entry = setup
    result = execute(
        tool,
        op="add",
        section="connections",
        entry={**entry, "target_id": entry["source_id"]},
    )
    assert not result.success
    assert "endpoints must be different resources" in result.error
    assert asyncio.run(ledger.list(LedgerSection.CONNECTIONS)) == []
