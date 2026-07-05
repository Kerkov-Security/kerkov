"""ReportTool — how an agent finishes with a structured payload.

Every agent has this tool, always — it's never whitelisted away. The
agent's system prompt instructs it to call `report` when done, partial,
or stuck. The `AgentRunner` configures the engine's `terminate_after_tool`
hook to break the loop after `report` lands, and reads the structured
fields off the `ToolCall` directly (not from the `ToolResult`).
"""

from typing import Literal, Optional, Type

from pydantic import Field

from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)

REPORT_TOOL_NAME = "report"


ReportStatus = Literal["completed", "partial", "stuck"]


class ReportParams(ToolParams):
    """Structured agent-finish payload.

    Pass the same fields back through to the master in `metadata` so the
    spawn tool can format the report without re-parsing strings.
    """

    status: ReportStatus = Field(description='One of: "completed", "partial", "stuck".')
    findings: str = Field(
        description="What was discovered or done. Concise, evidence-backed.",
    )
    next_steps: str = Field(
        default="",
        description="Recommended follow-up — especially useful when partial/stuck.",
    )


class ReportTool(BaseTool):
    """Records the agent's final structured report.

    `execute()` returns success unconditionally — the real signal is the
    `terminate_after_tool` hook on the LoopSession picking this up and
    breaking the loop. The structured payload is on the originating
    `ToolCall.parameters` and is what the runner uses.
    """

    def __init__(self) -> None:
        super().__init__(
            name=REPORT_TOOL_NAME,
            description=(
                "Finish the agent run with a structured report. Call this "
                "exactly once when the task is completed, partial, or stuck. "
                "Do not emit further tool calls after this."
            ),
        )

    def get_params_class(self) -> Type[ToolParams]:
        return ReportParams

    async def run(self, toolParams: ToolParams) -> ToolResult:
        assert isinstance(toolParams, ReportParams)
        return ToolResult(
            success=True,
            output=f"Reported: status={toolParams.status}",
            metadata={
                "status": toolParams.status,
                "findings": toolParams.findings,
                "next_steps": toolParams.next_steps,
            },
        )

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        return None
