"""Pydantic models for the agent subsystem."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ReportStatus = Literal["completed", "partial", "stuck"]


class AgentRunReport(BaseModel):
    """The structured outcome of an agent run, returned to the master.

    `engagement_id` is the JSONL file id; callers can open or `/resume`
    the full transcript with it.
    """

    status: ReportStatus
    findings: str
    next_steps: str = ""
    agent_type: str
    agent_run_id: str
    engagement_id: Optional[str] = None


class AgentProfile(BaseModel):
    """The reusable definition of an agent.

    `allowed_tools` is the MAXIMUM tool whitelist for any spawn of this
    profile. A spawn call may narrow the set (request a subset) but may
    never widen it — see `effective_tools()`.

    Sources are tracked so the manager can warn on duplicate names and so
    listings can distinguish built-in from user-defined profiles.
    """

    name: str = Field(description="Profile name (unique key)")
    description: str = Field(
        description="Short description shown to the master LLM in the index"
    )
    system_prompt: str = Field(
        description="Stripped-down, task-completion focused system prompt"
    )
    allowed_tools: List[str] = Field(description="Maximum tool whitelist — tool names")
    max_iterations: int = Field(
        default=30,
        description="Per-agent loop cap (lower than the master's 100)",
        gt=0,
    )
    source: str = Field(
        default="builtin",
        description='Where this profile came from: "builtin" or an AGENT.md path',
    )

    def effective_tools(self, requested: Optional[List[str]] = None) -> List[str]:
        """Resolve the effective tool set for a spawn against this profile."""
        if requested is None:
            return list(self.allowed_tools)
        requested_set = set(requested)
        return [name for name in self.allowed_tools if name in requested_set]
