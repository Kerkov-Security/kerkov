"""Interpret existing Cinc/InSpec evidence without running another scanner."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Type

from pydantic import ConfigDict, Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.compliance.reports import import_report
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ComplianceReportParams(ToolParams):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(
        min_length=1,
        max_length=4096,
        description="Local Cinc/InSpec JSON evidence. Relative paths stay in the engagement workspace; absolute local paths are accepted.",
    )
    expected_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Optional Cinc profile name required in the report.",
    )
    max_controls: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Maximum controls returned, with errors/failures first. Full results remain in the saved artifact.",
    )


class ComplianceReportTool(BaseTool):
    """Preserve scanner evidence and return bounded control-level observations."""

    def __init__(self) -> None:
        super().__init__(
            name="compliance_report",
            description=(
                "Import existing Cinc/InSpec JSON evidence and summarize control outcomes "
                "with evidence paths and a SHA-256. Prefer compliance_scan to assess an "
                "authorized SSH host with containerized Cinc Auditor. Keeps passed, failed, "
                "errored, skipped, empty and waived controls distinct. Import succeeds even "
                "when controls fail. No scanner execution, automatic remediation, certification "
                "or automatic ledger writes. Imported host identity is unverified; correlate "
                "with the reviewed inventory. Raw JSON and full normalized results stay in "
                "private workspace artifacts; summaries exclude raw test payloads and inputs."
            ),
        )

    def get_params_class(self) -> Type[ToolParams]:
        return ComplianceReportParams

    def approval_scope(self, params: dict[str, Any]) -> ApprovalScope:
        return ApprovalScope.exact_only(volatile=("max_controls",))

    def validate_params(self, toolParams: ToolParams) -> str | None:
        if not isinstance(toolParams, ComplianceReportParams):
            return "invalid compliance report parameters"
        for value in (toolParams.report, toolParams.expected_profile):
            if value is not None and (
                not value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                return "report paths and identifiers must be nonempty and contain no control characters"
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, ComplianceReportParams):
            raise TypeError("expected ComplianceReportParams")
        workspace = get_current_workspace()
        if workspace is None:
            return ToolResult(
                success=False,
                output="",
                error="Compliance reports require an active engagement workspace",
            )
        try:
            summary = await asyncio.to_thread(
                import_report,
                workspace.resolve(),
                toolParams.report,
                expected_profile=toolParams.expected_profile,
                max_controls=toolParams.max_controls,
            )
            return ToolResult(
                success=True,
                output=json.dumps(summary, indent=2),
                metadata={
                    key: value
                    for key, value in summary.items()
                    if key not in {"controls", "notice"}
                },
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
