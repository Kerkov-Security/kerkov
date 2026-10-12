"""Read-only resource profiles assembled from the engagement ledger.

Profiles never infer ownership from substrings, DNS or deployment relationships.
An observation belongs to a resource only when its explicit address matches that
resource's normalized identity. The graph supplies navigation to related resources.
"""

from typing import Any, Optional, Sequence

from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import project_resource_graph
from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerEntry,
    LedgerSection,
)
from kerkov.core.knowledge_base.resources import Resource

PROFILE_SECTIONS = (
    *RESOURCE_SECTIONS,
    LedgerSection.CONNECTIONS,
    LedgerSection.CONTAINERS,
    LedgerSection.HOSTS,
    LedgerSection.SERVICES,
    LedgerSection.FINDINGS,
)
MAX_PROFILE_ROWS = 200
MAX_PROFILE_TEXT = 8192
FACT_FIELDS = {
    LedgerSection.HOSTS: ("ip", "hostname", "os", "technologies", "note"),
    LedgerSection.SERVICES: (
        "host",
        "ip",
        "port",
        "protocol",
        "service",
        "version",
        "technologies",
        "note",
    ),
    LedgerSection.FINDINGS: (
        "title",
        "severity",
        "status",
        "host",
        "description",
        "note",
        "remediation",
    ),
}


def _matches(resource: Resource, value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return (
            Resource.from_stored({"kind": resource.kind, "value": value}).value
            == resource.value
        )
    except (ValidationError, ValueError):
        return False


def _fact(row: LedgerEntry) -> dict[str, Any]:
    fields = {}
    clipped = []
    for field in FACT_FIELDS[row.section]:
        value = row.entry.get(field)
        if type(value) in (str, int, float, bool):
            text = str(value)
            fields[field] = text[:MAX_PROFILE_TEXT]
            if len(text) > MAX_PROFILE_TEXT:
                clipped.append(field)
        elif field == "technologies" and isinstance(value, list):
            text = ", ".join(item for item in value if isinstance(item, str))
            fields[field] = text[:MAX_PROFILE_TEXT]
            if len(text) > MAX_PROFILE_TEXT:
                clipped.append(field)
    return {
        "id": row.id,
        "section": row.section.value,
        "entry": fields,
        "artifact_ref": row.artifact_ref,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "truncated_fields": clipped,
    }


def project_resource_profile(
    entries: Sequence[LedgerEntry], entry_id: int
) -> Optional[dict[str, Any]]:
    """Build one profile from a coherent snapshot, excluding secret sections."""
    rows = [row for row in entries if row.section in PROFILE_SECTIONS]
    graph = project_resource_graph(rows)
    node = next(
        (
            node
            for node in graph["nodes"]
            if node["kind"] in ("domain", "ip", "url", "repository")
            and entry_id in node["entry_ids"]
        ),
        None,
    )
    if node is None:
        return None
    resource = Resource.from_stored({"kind": node["kind"], "value": node["value"]})
    resource_ids = set(node["entry_ids"])
    records = [
        row
        for row in rows
        if row.section in RESOURCE_SECTIONS and row.id in resource_ids
    ]
    facts = []
    for row in rows:
        if row.section not in FACT_FIELDS:
            continue
        selectors = (
            ("ip", "hostname") if row.section is LedgerSection.HOSTS else ("host", "ip")
        )
        if any(_matches(resource, row.entry.get(field)) for field in selectors):
            facts.append(row)
    facts.sort(key=lambda row: (row.updated_at, row.id or 0), reverse=True)
    edges = [
        edge
        for edge in graph["edges"]
        if node["id"] in (edge["source"], edge["target"])
    ]
    connection_ids = {cid for edge in edges for cid in edge["connection_ids"]}
    connections = [
        row
        for row in rows
        if row.section is LedgerSection.CONNECTIONS and row.id in connection_ids
    ]
    related_ids = {
        endpoint for edge in edges for endpoint in (edge["source"], edge["target"])
    } - {node["id"]}
    related = [item for item in graph["nodes"] if item["id"] in related_ids]
    linked_rows = records + facts + connections
    activity = sorted(
        linked_rows, key=lambda row: (row.updated_at, row.id or 0), reverse=True
    )
    observed = [edge["observed_at"] for edge in edges if edge.get("observed_at")]
    groups = {
        "hosts": [_fact(row) for row in facts if row.section is LedgerSection.HOSTS],
        "services": [
            _fact(row) for row in facts if row.section is LedgerSection.SERVICES
        ],
        "findings": [
            _fact(row) for row in facts if row.section is LedgerSection.FINDINGS
        ],
        "activity": [
            {
                "id": row.id,
                "section": row.section.value,
                "label": (
                    row.entry.get("title")
                    or row.entry.get("service")
                    or row.entry.get("relation")
                    or row.entry.get("name")
                    or row.entry.get("hostname")
                    or row.entry.get("value")
                    or row.section.value
                ),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "artifact_ref": row.artifact_ref,
            }
            for row in activity
        ],
    }
    for item in groups["activity"]:
        item["label"] = str(item["label"])[:256]
    return {
        "resource": node,
        "records": [
            {
                "id": row.id,
                "section": row.section.value,
                "entry": Resource.from_stored(row.entry).model_dump(exclude_none=True),
                "artifact_ref": row.artifact_ref,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in records
        ],
        "last_observed_at": max(observed, default=None),
        "last_recorded_at": max((row.updated_at for row in linked_rows), default=None),
        "connections": edges[:MAX_PROFILE_ROWS],
        "related_resources": related[:MAX_PROFILE_ROWS],
        "counts": {
            **{key: len(value) for key, value in groups.items()},
            "connections": len(edges),
        },
        **{key: value[:MAX_PROFILE_ROWS] for key, value in groups.items()},
        "truncated": any(
            len(value) > MAX_PROFILE_ROWS
            for value in [*groups.values(), edges, related]
        ),
    }
