"""LedgerTool — record confirmed facts into the always-in-context findings ledger.

The ledger is the model's durable working memory: hosts, services, credentials,
findings, flags and next steps written here are re-injected at the top of context
every turn and survive context compaction verbatim. Anything the model does not
record here can be lost when the conversation is summarized.

Late-bound like `recall`: the ledger is keyed to the engagement, which does not
exist when tools are built, so both the ledger and the writing engagement id are
fetched through getters per call.
"""

import json
from typing import Any, Callable, Dict, Optional, Type

from pydantic import Field, ValidationInfo, field_validator

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.knowledge_base.connections import SQLITE_MAX_ID
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger, identity_error
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

LedgerGetter = Callable[[], Optional[FindingsLedger]]
EngagementIdGetter = Callable[[], Optional[str]]

_LEDGER_OP = ("add", "list", "remove", "graph")
GRAPH_PAGE_MAX_CHARS = 5000


def _connection_id(value: Any, field: str) -> int:
    """Accept integer-valued provider numbers without relaxing storage models."""
    # Gemini's protobuf Struct encodes every JSON number as a double. Normalize
    # only integral floats here; strings, booleans and fractional/nonfinite values
    # must still fail before a connection reaches the strict storage model.
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not 0 < value <= SQLITE_MAX_ID:
        raise ValueError(
            f"Connection {field} must be a positive integer within the SQLite id range."
        )
    return value


class LedgerToolParams(ToolParams):
    op: str = Field(
        description="'add' to record a fact, 'list' to read the ledger, "
        "'remove' to retract an entry by id, 'graph' to read resource nodes "
        "and their connections as compact JSON."
    )
    section: Optional[LedgerSection] = Field(
        default=None,
        description="Which section: assets (what we protect), targets (offensive "
        "assessment subjects), connections, containers, hosts, services, credentials, findings, "
        "flags, next_steps. Required for 'add'; an optional filter for 'list'.",
    )
    entry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="The record to store (for 'add'). Use conventional keys so "
        "entries merge by identity instead of duplicating; the keys "
        "marked * are required: assets/targets {kind*, value*, name, environment, "
        "tags, notes}; kind is "
        "domain, ip, url (HTTP/S), or repository (absolute local path or ~/path). "
        "Examples: assets {kind: 'repository', value: '/work/api'}, targets "
        "{kind: 'url', value: 'https://example.com'}. Resource names and context "
        "fields are optional. Environment is up to 128 characters, tags is a list "
        "of up to 20 strings of 64 characters without commas, and notes is up to "
        "4096 characters; "
        "other resource fields are rejected. connections {source_id, source_vantage, target_id*, "
        "relation*, provenance*, note}; use positive integer assets/targets ids "
        "from 'list' or 'graph'. relation is resolves_to (domain -> ip), deploys_to "
        "(repository -> domain/ip/url), or reaches (domain/ip or explicit vantage -> domain/ip). "
        "reaches requires exactly one source_id or source_vantage, protocol tcp|udp, "
        "port 1..65535, and optional service (max 80 chars). source_vantage is an "
        "explicit stable execution-environment label (max 200 chars), never an assumed runner. "
        "Observed reaches defaults observed_at to the evidence creation time. An explicit "
        "ISO8601 time with timezone must not be later than the evidence; declared reaches "
        "cannot have observed_at. Use source_id for IP addresses or existing hosts. Route fields "
        "are forbidden on other relations, which require source_id. "
        "provenance is observed (requires a valid "
        "top-level artifact_ref) or declared (requires a note describing the "
        "explicit user declaration or configuration). Never infer relationships "
        "from similar names. URLs, DNS, and deployment alone do not establish "
        "reachability. reaches records only "
        "direct transport reachability from that source, not authentication, "
        "forwarding, or a multi-hop route. Do not fabricate observation timestamps. "
        "uses_host edges are derived by 'graph' and cannot be added. "
        "hosts {ip*, hostname*, os, note} — at least "
        "one of ip/hostname; services {host*, port*, service, version, note}; "
        "credentials {username*, host*|source*, secret, kind, validated}; "
        "findings {title*, severity, host, note}; flags {value*, label, host}; "
        "next_steps {step*}.",
    )
    entry_id: Optional[int] = Field(
        default=None,
        description="Id of the entry to remove (for 'remove'; ids are shown by "
        "'list').",
    )
    artifact_ref: Optional[int] = Field(
        default=None,
        description="Artifact id linking an entry to its evidence (the N from "
        "an 'artifact#N' reference). Required for observed connections, optional "
        "otherwise. A connection's reference must be a positive integer id of "
        "an artifact that exists in this engagement's knowledge base.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="For 'graph', continue from the previous page's next_offset. "
        "Starts at 0; resources precede connections. Restart at 0 after edits.",
    )

    @field_validator("entry", mode="before")
    @classmethod
    def normalize_connection_ids(cls, value: Any, info: ValidationInfo) -> Any:
        if info.data.get("section") is LedgerSection.CONNECTIONS and isinstance(
            value, dict
        ):
            value = dict(value)
            for field in ("source_id", "target_id"):
                if field == "source_id" and value.get(field) is None:
                    continue
                if field in value:
                    value[field] = _connection_id(value[field], field)
            # Provider Structs also encode ports as doubles. Keep strict range
            # and boolean validation in the Connection storage model.
            if type(value.get("port")) is float and value["port"].is_integer():
                value["port"] = int(value["port"])
        return value

    @field_validator("artifact_ref", mode="before")
    @classmethod
    def validate_connection_artifact_ref(cls, value: Any, info: ValidationInfo) -> Any:
        # Preserve legacy coercion for other ledger sections.
        if info.data.get("section") is LedgerSection.CONNECTIONS and value is not None:
            return _connection_id(value, "artifact_ref")
        return value


class LedgerTool(BaseTool):
    """Write and read the engagement's structured findings ledger."""

    def __init__(
        self,
        ledger_getter: LedgerGetter,
        engagement_id_getter: EngagementIdGetter,
    ):
        super().__init__(
            name="ledger",
            description=(
                "Record confirmed facts into the findings ledger — your durable "
                "working memory. RECORD facts here the moment you confirm them: "
                "assets (what we protect), targets (offensive assessment subjects), "
                "connections between those resources, "
                "credentials, hosts, open services, findings, flags, and next "
                "steps. The ledger is re-injected at the top of context every "
                "turn and kept verbatim across context compaction, so a "
                "credential recorded now is still available much later even after "
                "the conversation is summarized; anything you do NOT record here "
                "may be lost. op='add' stores an entry (it merges by identity, so "
                "re-adding a host with more detail enriches it rather than "
                "duplicating). Asset/target roles do not change tool permissions. "
                "Record connections using existing resource ids from 'list': "
                "resolves_to links a domain to an IP, and deploys_to links a "
                "repository to a domain, IP, or URL. Observed connections need "
                "artifact evidence; declared connections need a note describing "
                "the explicit declaration or configuration. Never guess these "
                "relationships from naming similarity. "
                "reaches records a direct tcp/udp connection to a domain/IP and port "
                "from a domain/IP source_id or explicit stable source_vantage. "
                "Record reachability when the source matters, such as pivots or non-default "
                "vantages; use services for ordinary scan results instead of duplicating "
                "every open port as reaches. Observations require evidence; omit "
                "observed_at to use its creation time. Record explicit configuration "
                "as declared with a note. "
                "Reachability establishes neither authentication nor multi-hop access. "
                "op='list' returns the current ledger. "
                "op='remove' retracts an entry by id when a fact turns out wrong. "
                "op='graph' returns compact resource nodes and connections as JSON, "
                "combining Asset/Target roles on a single node and deriving "
                "uses_host edges from exact URL host matches. "
                "Container entry_ids identify observations, not connection endpoints. "
                "For resource nodes, entry_ids and edge source_id/target_id are usable "
                "resource ledger ids. Virtual "
                "vantage nodes have no ledger ids; their outgoing edges instead "
                "use source_vantage. Reachability edges include protocol, port, "
                "and any service/observed_at. Edge "
                "connection_ids remove recorded connections. Continue with "
                "offset=next_offset until next_offset is null. Large resource "
                "text may be omitted with omitted_fields identifying those fields. "
                "Use 'list' with "
                "section='connections' to read their full notes and evidence."
            ),
        )
        self._ledger_getter = ledger_getter
        self._engagement_id_getter = engagement_id_getter

    def get_params_class(self) -> Type[ToolParams]:
        return LedgerToolParams

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        """Widen to the whole tool: writes only to local engagement state."""
        return ApprovalScope.whole_tool()

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        assert isinstance(toolParams, LedgerToolParams)
        if toolParams.op not in _LEDGER_OP:
            return f"op must be one of {', '.join(_LEDGER_OP)}."
        if toolParams.op == "add":
            if toolParams.section is None:
                return "add requires a section."
            if not toolParams.entry:
                return "add requires a non-empty entry."
            missing = identity_error(toolParams.section, toolParams.entry)
            if missing:
                return missing
            if (
                toolParams.section is LedgerSection.CONNECTIONS
                and toolParams.entry.get("provenance") == "observed"
                and toolParams.artifact_ref is None
            ):
                return "Observed connections require an artifact_ref."
        if toolParams.op == "remove" and toolParams.entry_id is None:
            return "remove requires an entry_id."
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        assert isinstance(toolParams, LedgerToolParams)
        ledger = self._ledger_getter()
        if ledger is None:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "No knowledge base is active for this session, so there is "
                    "no findings ledger to write to."
                ),
            )

        if toolParams.op == "add":
            return await self._add(ledger, toolParams)
        if toolParams.op == "remove":
            return await self._remove(ledger, toolParams)
        if toolParams.op == "graph":
            return await self._graph(ledger, toolParams.offset)
        return await self._list(ledger, toolParams)

    async def _add(
        self, ledger: FindingsLedger, params: LedgerToolParams
    ) -> ToolResult:
        assert params.section is not None and params.entry is not None
        engagement_id = self._engagement_id_getter() or ledger._kb.root_engagement_id
        try:
            if params.section is LedgerSection.CONNECTIONS:
                result = await ledger.add_connection(
                    params.entry, artifact_ref=params.artifact_ref
                )
                entry_id = result.entry_id
                if result.outcome == "observation_retained":
                    return ToolResult(
                        success=True,
                        output=(
                            f"Kept observed connection #{entry_id} and its evidence. "
                            "The declaration and its note were not stored."
                        ),
                        metadata={
                            "entry_id": entry_id,
                            "section": params.section.value,
                            "outcome": result.outcome,
                        },
                    )
                if result.outcome == "stale_observation_retained":
                    return ToolResult(
                        success=True,
                        output=(
                            f"Kept newer observed connection #{entry_id} and its evidence. "
                            "The older observation was not stored."
                        ),
                        metadata={
                            "entry_id": entry_id,
                            "section": params.section.value,
                            "outcome": result.outcome,
                        },
                    )
            else:
                entry_id = await ledger.add(
                    params.section,
                    params.entry,
                    engagement_id=engagement_id,
                    artifact_ref=params.artifact_ref,
                )
        except ValueError as exc:
            if params.section is not LedgerSection.CONNECTIONS:
                raise
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=True,
            output=f"Recorded to {params.section.value} ledger as entry #{entry_id}.",
            metadata={"entry_id": entry_id, "section": params.section.value},
        )

    async def _remove(
        self, ledger: FindingsLedger, params: LedgerToolParams
    ) -> ToolResult:
        assert params.entry_id is not None
        removed = await ledger.remove(params.entry_id)
        if not removed:
            return ToolResult(
                success=False,
                output="",
                error=f"No ledger entry #{params.entry_id}.",
            )
        return ToolResult(
            success=True,
            output=f"Removed ledger entry #{params.entry_id}.",
            metadata={"entry_id": params.entry_id},
        )

    async def _list(
        self, ledger: FindingsLedger, params: LedgerToolParams
    ) -> ToolResult:
        entries = await ledger.list(params.section)
        if not entries:
            where = f" in {params.section.value}" if params.section else ""
            return ToolResult(
                success=True,
                output=f"The findings ledger is empty{where}.",
                metadata={"count": 0},
            )
        lines = [
            f"#{e.id} [{e.section.value}] {e.entry}"
            + (f" (artifact#{e.artifact_ref})" if e.artifact_ref is not None else "")
            for e in entries
        ]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"count": len(entries)},
        )

    async def _graph(self, ledger: FindingsLedger, offset: int) -> ToolResult:
        graph = await ledger.graph()
        # Keep the complete hash-addressed graph API for application clients;
        # model output needs only actionable ledger ids, without repeated hashes
        # or evidence notes that can hide edges behind generic output clipping.
        graph_nodes = {node["id"]: node for node in graph["nodes"]}
        nodes = []
        for node in graph["nodes"]:
            compact_node = {
                field: node[field] for field in ("kind", "value", "roles", "entry_ids")
            }
            if node["kind"] == "container":
                compact_node["state"] = node["container"]["state"]
                compact_node["ready"] = node["container"]["ready"]
            names = {
                record["role"]: record["name"]
                for record in node["records"]
                if record["name"] is not None
            }
            if names:
                compact_node["names"] = names
            nodes.append(compact_node)
        edges = []
        for edge in graph["edges"]:
            compact_edge = {
                "target_id": graph_nodes[edge["target"]]["entry_ids"][0],
                "relation": edge["relation"],
                "connection_ids": edge["connection_ids"],
                "provenances": edge["provenances"],
            }
            source = graph_nodes[edge["source"]]
            if source["kind"] == "vantage":
                compact_edge["source_vantage"] = source["value"]
            else:
                compact_edge["source_id"] = source["entry_ids"][0]
            for field in ("protocol", "port", "service", "observed_at"):
                if field in edge:
                    compact_edge[field] = edge[field]
            artifact_refs = sorted(
                {
                    evidence["artifact_ref"]
                    for evidence in edge["evidence"]
                    if evidence["artifact_ref"] is not None
                }
            )
            if artifact_refs:
                compact_edge["artifact_refs"] = artifact_refs
            edges.append(compact_edge)
        items = [("nodes", node) for node in nodes] + [
            ("edges", edge) for edge in edges
        ]
        if offset > len(items):
            return ToolResult(
                success=False,
                output="",
                error="Graph offset is beyond the inventory; restart with offset=0.",
            )
        page: Dict[str, Any] = {"nodes": [], "edges": [], "next_offset": None}
        output = json.dumps(page, ensure_ascii=True, separators=(",", ":"))
        for index in range(offset, len(items)):
            section, item = items[index]
            page[section].append(item)
            page["next_offset"] = index + 1 if index + 1 < len(items) else None
            candidate = json.dumps(page, ensure_ascii=True, separators=(",", ":"))
            if len(candidate) > GRAPH_PAGE_MAX_CHARS:
                if index > offset:
                    break
                # A single unbounded URL/path/name must not prevent pagination.
                # Keep its handles and mark omitted text instead of shortening
                # resource identities into plausible but incorrect values.
                item = {
                    key: value
                    for key, value in item.items()
                    if key not in {"value", "names"}
                }
                item["omitted_fields"] = [
                    key for key in ("value", "names") if key in items[index][1]
                ]
                page[section][-1] = item
                candidate = json.dumps(page, ensure_ascii=True, separators=(",", ":"))
                if len(candidate) > GRAPH_PAGE_MAX_CHARS:
                    return ToolResult(
                        success=False,
                        output="",
                        error="Graph record exceeds the page budget; inspect the ledger with list.",
                    )
            output = candidate
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "node_count": len(graph["nodes"]),
                "edge_count": len(graph["edges"]),
            },
        )
