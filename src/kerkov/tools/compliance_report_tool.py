"""Interpret existing Cinc or OpenSCAP evidence without running a scanner."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Type

from pydantic import ConfigDict, Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.compliance.openscap import import_report as import_openscap_report
from kerkov.core.compliance.reports import ReportImportError, import_report
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ComplianceReportParams(ToolParams):
    model_config = ConfigDict(extra="forbid")

    report: str = Field(
        min_length=1,
        max_length=4096,
        description="Local Cinc/InSpec JSON or OpenSCAP XCCDF/ARF XML evidence. Relative paths stay in the engagement workspace; absolute local paths are accepted.",
    )
    format: Literal["cinc", "openscap"] = Field(
        default="cinc",
        description="Report format. Choose openscap for XCCDF or ARF XML.",
    )
    expected_profile: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Optional required Cinc profile name or full XCCDF profile ID.",
    )
    result_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="Exact XCCDF TestResult ID, required for OpenSCAP reports with multiple assessments.",
    )
    max_controls: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Maximum controls or XCCDF rule results returned, with errors/failures first. Full results remain in the saved artifact.",
    )


class ComplianceReportTool(BaseTool):
    """Preserve scanner evidence and return bounded control-level observations."""

    def __init__(self) -> None:
        super().__init__(
            name="compliance_report",
            description=(
                "Import existing Cinc/InSpec JSON or OpenSCAP XCCDF/ARF XML evidence "
                "and summarize outcomes with evidence paths and a SHA-256. Set "
                "format=openscap for XML. Import succeeds even "
                "when controls fail. No scanner execution, automatic remediation, certification "
                "or automatic ledger writes. Imported host identity is unverified; correlate "
                "with the reviewed inventory. Raw evidence and full normalized results stay in "
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
        if toolParams.result_id is not None and toolParams.format != "openscap":
            return "result_id requires format=openscap"
        for value in (
            toolParams.report,
            toolParams.expected_profile,
            toolParams.result_id,
        ):
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
            workspace = workspace.resolve()
            if toolParams.format == "openscap":
                summary = await asyncio.to_thread(
                    import_openscap_report,
                    workspace,
                    toolParams.report,
                    expected_profile=toolParams.expected_profile,
                    result_id=toolParams.result_id,
                    max_rules=toolParams.max_controls,
                )
            else:
                summary = await asyncio.to_thread(
                    import_report,
                    workspace,
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
                    if key not in {"controls", "rules", "notice"}
                },
            )
        except ReportImportError as exc:
            return ToolResult(
                success=False,
                output=json.dumps(exc.metadata),
                error=str(exc),
                metadata=exc.metadata,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
