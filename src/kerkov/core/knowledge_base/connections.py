"""Validated resource connections and a deterministic inventory graph projection."""

import hashlib
import json
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any, Dict, Iterable, Literal, Optional
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from kerkov.core.knowledge_base.resources import Resource

if TYPE_CHECKING:
    from kerkov.core.knowledge_base.models import LedgerEntry


SQLITE_MAX_ID = (1 << 63) - 1


class Connection(BaseModel):
    """An assertion between two resource ledger rows.

    The knowledge base validates endpoint existence and artifact evidence while
    writing, so references are checked in the same transaction as the assertion.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: int = Field(gt=0, le=SQLITE_MAX_ID)
    target_id: int = Field(gt=0, le=SQLITE_MAX_ID)
    relation: Literal["resolves_to", "deploys_to"]
    provenance: Literal["observed", "declared"]
    note: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("connection note cannot contain control characters")
        value = value.strip()
        if not value:
            raise ValueError("connection note cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_assertion(self) -> "Connection":
        if self.source_id == self.target_id:
            raise ValueError("connection endpoints must be different resources")
        if self.provenance == "declared" and self.note is None:
            raise ValueError("declared connections require a note")
        return self


def validate_connection_resources(
    connection: Connection, source: Resource, target: Resource
) -> None:
    """Reject self-connections and relations whose endpoint kinds do not fit."""
    if (source.kind, source.value) == (target.kind, target.value):
        raise ValueError("connection endpoints must be different resources")
    if connection.relation == "resolves_to":
        if source.kind != "domain" or target.kind != "ip":
            raise ValueError("resolves_to requires a domain source and an IP target")
    elif source.kind != "repository" or target.kind not in {"url", "domain", "ip"}:
        raise ValueError(
            "deploys_to requires a repository source and a URL, domain, or IP target"
        )


def _opaque_id(prefix: str, *identity: str) -> str:
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _valid_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= SQLITE_MAX_ID


def project_resource_graph(entries: Iterable["LedgerEntry"]) -> Dict[str, Any]:
    """Project inventory and connection rows into JSON-safe nodes and edges.

    Asset/target identities share one node, with role-specific names retained in
    records. Only exact URL host matches produce derived edges; DNS resolution
    and deployment are never inferred. Invalid historical and dangling rows are
    omitted, and evidence contains references, never artifact bodies.
    """
    rows: list[tuple[int, "LedgerEntry"]] = []
    for row in entries:
        row_id = row.id
        if type(row_id) is int and 0 < row_id <= SQLITE_MAX_ID:
            rows.append((row_id, row))
    rows.sort(key=lambda item: item[0])
    resources: Dict[int, Resource] = {}
    node_for_entry: Dict[int, str] = {}
    nodes: Dict[str, Dict[str, Any]] = {}
    identity_nodes: Dict[tuple, str] = {}
    for row_id, row in rows:
        if row.section not in {"assets", "targets"}:
            continue
        try:
            resource = Resource.from_stored(row.entry)
        except (ValidationError, ValueError):
            continue
        identity = (resource.kind, resource.value)
        node_id = _opaque_id("resource", *identity)
        role = "asset" if row.section == "assets" else "target"
        resources[row_id] = resource
        node_for_entry[row_id] = node_id
        identity_nodes[identity] = node_id
        node = nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": resource.kind,
                "value": resource.value,
                "roles": [],
                "entry_ids": [],
                "records": [],
            },
        )
        if role not in node["roles"]:
            node["roles"].append(role)
        node["entry_ids"].append(row_id)
        node["records"].append(
            {"entry_id": row_id, "role": role, "name": resource.name}
        )

    edges: Dict[tuple, Dict[str, Any]] = {}

    def edge_for(source: str, relation: str, target: str) -> Dict[str, Any]:
        return edges.setdefault(
            (source, relation, target),
            {
                "id": _opaque_id("connection", source, relation, target),
                "source": source,
                "target": target,
                "relation": relation,
                "connection_ids": [],
                "provenances": [],
                "evidence": [],
            },
        )

    for row_id, row in rows:
        if row.section != "connections":
            continue
        try:
            connection = Connection.model_validate(row.entry)
            source = resources[connection.source_id]
            target = resources[connection.target_id]
            validate_connection_resources(connection, source, target)
        except (ValidationError, ValueError, KeyError):
            continue
        if connection.provenance == "observed" and not _valid_id(row.artifact_ref):
            continue
        edge = edge_for(
            node_for_entry[connection.source_id],
            connection.relation,
            node_for_entry[connection.target_id],
        )
        edge["connection_ids"].append(row_id)
        if connection.provenance not in edge["provenances"]:
            edge["provenances"].append(connection.provenance)
        edge["evidence"].append(
            {
                "connection_id": row_id,
                "provenance": connection.provenance,
                "artifact_ref": row.artifact_ref,
                "note": connection.note,
            }
        )

    for node in nodes.values():
        node["roles"].sort()
        if node["kind"] != "url":
            continue
        host = urlsplit(node["value"]).hostname
        try:
            host_identity = ("ip", str(ip_address(host)))
        except ValueError:
            host_identity = ("domain", host)
        target_id = identity_nodes.get(host_identity)
        if target_id is not None:
            edge_for(node["id"], "uses_host", target_id)["provenances"] = ["derived"]

    for edge in edges.values():
        edge["provenances"].sort()
    return {
        "nodes": sorted(nodes.values(), key=lambda node: (node["kind"], node["value"])),
        "edges": [edges[key] for key in sorted(edges)],
    }
