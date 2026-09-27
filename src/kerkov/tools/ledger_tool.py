"""LedgerTool — record confirmed facts into the always-in-context findings ledger.

The ledger is the model's durable working memory: hosts, services, credentials,
findings, flags and next steps written here are re-injected at the top of context
every turn and survive context compaction verbatim. Anything the model does not
record here can be lost when the conversation is summarized.

Late-bound like `recall`: the ledger is keyed to the engagement, which does not
exist when tools are built, so both the ledger and the writing engagement id are
fetched through getters per call.
"""

from typing import Any, Callable, Dict, Optional, Type

from pydantic import Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger, identity_error
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

LedgerGetter = Callable[[], Optional[FindingsLedger]]
EngagementIdGetter = Callable[[], Optional[str]]

_LEDGER_OP = ("add", "list", "remove")


class LedgerToolParams(ToolParams):
    op: str = Field(
        description="'add' to record a fact, 'list' to read the ledger, "
        "'remove' to retract an entry by id."
    )
    section: Optional[LedgerSection] = Field(
        default=None,
        description="Which section: assets (what we protect), targets (offensive "
        "assessment subjects), hosts, services, credentials, findings, "
        "flags, next_steps. Required for 'add'; an optional filter for 'list'.",
    )
    entry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="The record to store (for 'add'). Use conventional keys so "
        "entries merge by identity instead of duplicating; the identity keys "
        "(marked *) are required: assets/targets {kind*, value*, name}; kind is "
        "domain, ip, url (HTTP/S), or repository (absolute local path or ~/path). "
        "Examples: assets {kind: 'repository', value: '/work/api'}, targets "
        "{kind: 'url', value: 'https://example.com'}. Resource names are optional; "
        "other resource fields are rejected. hosts {ip*, hostname*, os, note} — at least "
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
        description="Optional artifact id to link a finding to its evidence "
        "(the N from an 'artifact#N' reference).",
    )


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
                "credentials, hosts, open services, findings, flags, and next "
                "steps. The ledger is re-injected at the top of context every "
                "turn and kept verbatim across context compaction, so a "
                "credential recorded now is still available much later even after "
                "the conversation is summarized; anything you do NOT record here "
                "may be lost. op='add' stores an entry (it merges by identity, so "
                "re-adding a host with more detail enriches it rather than "
                "duplicating). Asset/target roles do not change tool permissions. "
                "op='list' returns the current ledger. "
                "op='remove' retracts an entry by id when a fact turns out wrong."
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
        return await self._list(ledger, toolParams)

    async def _add(
        self, ledger: FindingsLedger, params: LedgerToolParams
    ) -> ToolResult:
        assert params.section is not None and params.entry is not None
        engagement_id = self._engagement_id_getter() or ledger._kb.root_engagement_id
        entry_id = await ledger.add(
            params.section,
            params.entry,
            engagement_id=engagement_id,
            artifact_ref=params.artifact_ref,
        )
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
