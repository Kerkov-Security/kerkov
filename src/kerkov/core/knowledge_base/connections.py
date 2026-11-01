"""Validated resource connections and a deterministic inventory graph projection."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any, Dict, Iterable, Literal, Optional
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from kerkov.core.knowledge_base.containers import ContainerObservation, access_host
from kerkov.core.knowledge_base.resources import Resource

if TYPE_CHECKING:
    from kerkov.core.knowledge_base.models import LedgerEntry


SQLITE_MAX_ID = (1 << 63) - 1
MAX_FACT_TEXT = 512
HOST_FACT_FIELDS = ("ip", "hostname", "os", "technologies", "note")
SERVICE_FACT_FIELDS = ("protocol", "service", "version", "technologies", "note")


@dataclass(frozen=True)
class ConnectionWriteResult:
    entry_id: int
    outcome: Literal["recorded", "observation_retained", "stale_observation_retained"]


class Connection(BaseModel):
    """An assertion between resources, or from an explicit reachability source.

    The knowledge base validates endpoint existence and artifact evidence while
    writing, so references are checked in the same transaction as the assertion.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: Optional[int] = Field(default=None, gt=0, le=SQLITE_MAX_ID)
    source_vantage: Optional[str] = Field(default=None, max_length=200)
    target_id: int = Field(gt=0, le=SQLITE_MAX_ID)
    relation: Literal["resolves_to", "deploys_to", "reaches"]
    provenance: Literal["observed", "declared"]
    note: Optional[str] = Field(default=None, max_length=2000)
    protocol: Optional[Literal["tcp", "udp"]] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    service: Optional[str] = Field(default=None, max_length=80)
    observed_at: Optional[str] = None

    @field_validator("note", "source_vantage", "service")
    @classmethod
    def validate_note(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(
                f"connection {info.field_name} cannot contain control characters"
            )
        value = value.strip()
        if not value:
            raise ValueError(f"connection {info.field_name} cannot be empty")
        if info.field_name == "source_vantage":
            try:
                ip_address(value)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "source_vantage cannot be an IP address; use source_id"
                )
        return value

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
            r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
            value,
        ):
            raise ValueError("observed_at must be an ISO8601 timestamp with timezone")
        try:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return observed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError) as exc:
            raise ValueError("observed_at must be a valid timestamp") from exc

    @model_validator(mode="after")
    def validate_assertion(self) -> "Connection":
        if self.relation == "reaches":
            if (self.source_id is None) == (self.source_vantage is None):
                raise ValueError(
                    "reaches requires exactly one source_id or source_vantage"
                )
            if self.protocol is None or self.port is None:
                raise ValueError("reaches requires protocol and port")
            if self.provenance == "declared" and self.observed_at is not None:
                raise ValueError("declared reaches cannot have observed_at")
        else:
            if self.model_fields_set & {
                "source_vantage",
                "protocol",
                "port",
                "service",
                "observed_at",
            }:
                raise ValueError("reachability fields are only allowed for reaches")
            if self.source_id is None:
                raise ValueError("connection requires source_id")
        if self.source_id == self.target_id:
            raise ValueError("connection endpoints must be different resources")
        if self.provenance == "declared" and self.note is None:
            raise ValueError("declared connections require a note")
        return self


def validate_connection_resources(
    connection: Connection, source: Optional[Resource], target: Resource
) -> None:
    """Reject self-connections and relations whose endpoint kinds do not fit."""
    if source is not None and (source.kind, source.value) == (
        target.kind,
        target.value,
    ):
        raise ValueError("connection endpoints must be different resources")
    if connection.relation == "reaches":
        if target.kind not in {"domain", "ip"} or (
            source is not None and source.kind not in {"domain", "ip"}
        ):
            raise ValueError("reaches requires domain or IP resources")
        return
    if source is None:
        raise ValueError("connection requires a source resource")
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


def _host_identity(value: Any) -> Optional[tuple[str, str]]:
    if not isinstance(value, str):
        return None
    for kind in ("ip", "domain"):
        try:
            return kind, Resource.from_stored({"kind": kind, "value": value}).value
        except (ValidationError, ValueError):
            continue
    return None


def _fact_fields(entry: Dict[str, Any], fields: tuple[str, ...]) -> Dict[str, str]:
    facts = {}
    for field in fields:
        value = entry.get(field)
        if field == "technologies" and isinstance(value, list):
            value = ", ".join(item for item in value if isinstance(item, str))
        if type(value) in (str, int, float) and str(value).strip():
            facts[field] = str(value).strip()[:MAX_FACT_TEXT]
    return facts


def _service_port(entry: Dict[str, Any]) -> Optional[tuple[int, Optional[str]]]:
    value = entry.get("port")
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,5})(?:/(tcp|udp))?", value.strip().lower())
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        return None
    return int(match.group(1)), match.group(2)


def _attach_host_facts(
    rows: list[tuple[int, "LedgerEntry"]],
    nodes: Dict[str, Dict[str, Any]],
    identity_nodes: Dict[tuple, str],
) -> None:
    """Fold hosts and services rows onto the node for the same address.

    Services become a list on their host, never edges: a services row has no
    source vantage or evidence, and ``reaches`` must keep meaning an observed
    route. Facts never add roles, so the asset/target vocabulary stays intact.
    """
    # Facts may also join by a hostname a hosts row tied to an IP; resource
    # nodes and derived uses_host edges keep matching exact identities only.
    host_nodes = dict(identity_nodes)

    def node_for(*values: Any) -> Optional[Dict[str, Any]]:
        identities = list(dict.fromkeys(filter(None, map(_host_identity, values))))
        if not identities:
            return None
        node_id = next((host_nodes[i] for i in identities if i in host_nodes), None)
        if node_id is None:
            kind, value = identities[0]
            node_id = _opaque_id("resource", kind, value)
            nodes[node_id] = {
                "id": node_id,
                "kind": kind,
                "value": value,
                "roles": [],
                "entry_ids": [],
                "records": [],
            }
        for identity in identities:
            host_nodes.setdefault(identity, node_id)
        return nodes[node_id]

    for row_id, row in rows:
        if row.section != "hosts" or not isinstance(row.entry, dict):
            continue
        node = node_for(row.entry.get("ip"), row.entry.get("hostname"))
        if node is not None:
            node["records"].append(
                {
                    "entry_id": row_id,
                    "section": "hosts",
                    **_fact_fields(row.entry, HOST_FACT_FIELDS),
                }
            )
    for row_id, row in rows:
        if row.section != "services" or not isinstance(row.entry, dict):
            continue
        port = _service_port(row.entry)
        node = node_for(row.entry.get("ip"), row.entry.get("host"))
        if port is None or node is None:
            continue
        service = {"entry_id": row_id, "port": port[0]}
        service.update(_fact_fields(row.entry, SERVICE_FACT_FIELDS))
        service["protocol"] = service.get("protocol", port[1] or "").lower()
        if not service["protocol"]:
            del service["protocol"]
        node.setdefault("services", []).append(service)
    for node in nodes.values():
        if "services" in node:
            node["services"].sort(
                key=lambda item: (item["port"], item.get("protocol", ""))
            )


def project_resource_graph(
    entries: Iterable["LedgerEntry"], *, include_host_facts: bool = False
) -> Dict[str, Any]:
    """Project inventory and connection rows into JSON-safe nodes and edges.

    Asset/target identities share one node, with role-specific names retained in
    records. Exact URL host matches and running containers' published ports
    produce derived edges; DNS resolution and deployment are never inferred.
    Invalid historical and dangling rows are omitted, and evidence contains
    references, never artifact bodies. ``include_host_facts`` adds hosts and
    services rows for the network map; their host-only nodes have no
    ``entry_ids``, so ledger-id consumers such as ``ledger op=graph`` omit them.
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
            {
                "entry_id": row_id,
                "role": role,
                "name": resource.name,
                **resource.model_dump(
                    include={"environment", "tags", "notes"}, exclude_none=True
                ),
            }
        )
    if include_host_facts:
        _attach_host_facts(rows, nodes, identity_nodes)

    edges: Dict[tuple, Dict[str, Any]] = {}

    def edge_for(
        source: str,
        relation: str,
        target: str,
        connection: Optional[Connection] = None,
        *,
        identity_suffix: tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        identity: tuple[str, ...] = (source, relation, target)
        if connection is not None and relation == "reaches":
            identity += (str(connection.protocol), str(connection.port))
        identity += identity_suffix
        return edges.setdefault(
            identity,
            {
                "id": _opaque_id("connection", *identity),
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
            source = resources[connection.source_id] if connection.source_id else None
            target = resources[connection.target_id]
            validate_connection_resources(connection, source, target)
        except (ValidationError, ValueError, KeyError):
            continue
        if connection.provenance == "observed" and (
            not _valid_id(row.artifact_ref)
            or (connection.relation == "reaches" and connection.observed_at is None)
        ):
            continue
        if connection.source_vantage is not None:
            source_node = _opaque_id("vantage", connection.source_vantage)
            nodes.setdefault(
                source_node,
                {
                    "id": source_node,
                    "kind": "vantage",
                    "value": connection.source_vantage,
                    "roles": [],
                    "entry_ids": [],
                    "records": [],
                },
            )
        else:
            source_node = node_for_entry[connection.source_id]
        edge = edge_for(
            source_node,
            connection.relation,
            node_for_entry[connection.target_id],
            connection,
        )
        edge["connection_ids"].append(row_id)
        if connection.provenance not in edge["provenances"]:
            edge["provenances"].append(connection.provenance)
        evidence = {
            "connection_id": row_id,
            "provenance": connection.provenance,
            "artifact_ref": row.artifact_ref,
            "note": connection.note,
        }
        if connection.relation == "reaches":
            edge.update(protocol=connection.protocol, port=connection.port)
            for field in ("service", "observed_at"):
                value = getattr(connection, field)
                if value is not None:
                    evidence[field] = value
            # Historical role duplicates can contribute several observations.
            # Surface metadata from the freshest observation, never a declaration
            # or a late write of older evidence. Keep every assertion in evidence.
            prior_time = edge.get("observed_at")
            is_fresher = connection.observed_at is not None and (
                prior_time is None
                or datetime.fromisoformat(connection.observed_at)
                >= datetime.fromisoformat(prior_time)
            )
            if is_fresher or (prior_time is None and connection.observed_at is None):
                edge.pop("service", None)
                if connection.service is not None:
                    edge["service"] = connection.service
                if connection.observed_at is not None:
                    edge["observed_at"] = connection.observed_at
        edge["evidence"].append(evidence)

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

    # Containers retain their own identity even when every published endpoint
    # uses the same loopback host. Published mappings are distinct from reaches:
    # Docker's port configuration does not prove network access.
    for row_id, row in rows:
        if row.section != "containers":
            continue
        try:
            observation = ContainerObservation.model_validate(row.entry)
        except (ValidationError, ValueError):
            continue
        node_id = _opaque_id("container", observation.identity())
        nodes[node_id] = {
            "id": node_id,
            "kind": "container",
            "value": observation.name,
            "roles": ["target"],
            "entry_ids": [row_id],
            "records": [],
            "container": observation.model_dump(exclude={"target_ids"}),
            "endpoint_ids": sorted(
                {
                    node_for_entry[entry_id]
                    for entry_id in observation.target_ids
                    if entry_id in node_for_entry
                }
            ),
        }
        if observation.state != "running":
            continue
        for port in observation.ports:
            try:
                host = access_host(port.host)
            except ValueError:
                continue  # Invalid historical binds must not break the map.
            source = next(
                (
                    node_for_entry[entry_id]
                    for entry_id in observation.target_ids
                    if entry_id in resources
                    and resources[entry_id].kind == "ip"
                    and resources[entry_id].value == host
                ),
                None,
            )
            if source is None:
                continue
            edge = edge_for(
                source,
                "publishes_port",
                node_id,
                identity_suffix=(
                    port.host,
                    port.protocol,
                    str(port.host_port),
                    str(port.container_port),
                ),
            )
            edge.update(
                provenances=["derived"],
                protocol=port.protocol,
                port=port.host_port,
                container_port=port.container_port,
                bind_host=port.host,
                observed_at=observation.observed_at,
                evidence=[{"container_id": row_id, "provenance": "derived"}],
            )

    for edge in edges.values():
        edge["provenances"].sort()
    return {
        "nodes": sorted(nodes.values(), key=lambda node: (node["kind"], node["value"])),
        "edges": [edges[key] for key in sorted(edges)],
    }
