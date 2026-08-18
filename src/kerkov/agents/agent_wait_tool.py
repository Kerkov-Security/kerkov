"""AgentWaitTool — block until detached agents report back.

The collection half of the detached `delegate` contract. It is the model's
in-turn path to an agent's findings; the supervisor's turn-boundary delivery is
the safety net for when the model never calls it.
"""

from typing import List, Optional, Type

from pydantic import Field

from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.report_format import build_report_result
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

WAIT_TOOL_NAME = "agent_wait"


class AgentWaitParams(ToolParams):
    """Which agents to wait for, and for how long."""

    ids: Optional[List[str]] = Field(
        default=None,
        description='Agent ids to wait for (e.g. ["a1", "a2"]). '
        "Omit to wait for every agent whose report you have not seen yet.",
    )
    timeout_s: Optional[float] = Field(
        default=None,
        description="Give up waiting after this many seconds and return the "
        "reports that are ready. Omit to wait until every agent finishes.",
        gt=0,
    )


class AgentWaitTool(BaseTool):
    """Collect the reports of agents spawned by `delegate`."""

    def __init__(self, supervisor: AgentSupervisor):
        super().__init__(
            name=WAIT_TOOL_NAME,
            description=(
                "Wait for agents you delegated and read their reports. "
                "Pass the ids `delegate` gave you, or omit `ids` to collect "
                "every report you have not seen. A timeout returns whatever "
                "is ready plus the ids still running."
            ),
        )
        self._supervisor = supervisor

    def get_params_class(self) -> Type[ToolParams]:
        return AgentWaitParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        assert isinstance(toolParams, AgentWaitParams)
        outcome = await self._supervisor.wait(
            ids=toolParams.ids, timeout=toolParams.timeout_s
        )
        # A timeout that strands finished agents' reports would lose work, so
        # a partial collection is a success, not an error.
        return build_report_result(
            outcome.reports,
            still_running=outcome.pending,
            unknown=outcome.unknown,
        )
