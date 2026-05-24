"""SpawnAgentTool — the master delegates a batch of self-contained tasks.

One tool call carries N task specs and launches them concurrently via
`asyncio.gather`. The caller (master or nested agent) blocks until every
spawned agent reports back, then receives all reports together.

Concurrency lives inside a single tool call by design: the conversation
engine executes a turn's tool calls serially, so N separate spawn calls
would run one agent fully before starting the next. Batching keeps the
fan-out where it belongs (see plan §7).
"""

import asyncio
from typing import Callable, List, Optional, Type

from pydantic import BaseModel, Field

from src.agents.agent_manager import AgentManager
from src.agents.agent_registry import AgentRegistry, BudgetExceeded
from src.agents.agent_runner import AgentRunReport, AgentRunner
from src.cli.engagement_store import EngagementStore
from src.core.logging.logger_factory import get_logger
from src.providers.base_provider import BaseLLMProvider
from src.tools.base_tool import (
    BaseTool,
    ToolApprovalLevel,
    ToolParams,
    ToolResult,
)


ProviderGetter = Callable[[], Optional[BaseLLMProvider]]


logger = get_logger("SpawnAgentTool")


SPAWN_TOOL_NAME = "delegate"


class AgentTaskSpec(BaseModel):
    """One delegated task in a `delegate` batch."""

    agent_type: str = Field(
        description="Profile name: built-in (recon, analysis, exploit, general) "
        "or a user-defined AGENT.md."
    )
    instruction: str = Field(
        description="Clear, self-contained task for the agent."
    )
    success_criteria: str = Field(
        default="",
        description="How the agent knows it is done. Optional but recommended.",
    )
    allowed_tools: Optional[List[str]] = Field(
        default=None,
        description="Optional subset of the profile's tools; can only narrow, "
        "never widen, the profile's whitelist.",
    )


class SpawnAgentParams(ToolParams):
    """Batch payload for `delegate`."""

    tasks: List[AgentTaskSpec] = Field(
        description="One or more tasks to delegate. All run concurrently; "
        "the caller blocks until every agent reports back.",
        min_length=1,
    )


class SpawnAgentTool(BaseTool):
    """Delegate one or more isolated sub-tasks to agents.

    Built fresh on each construction with the caller's depth: master
    constructs at `parent_depth=0`; a nested instance handed to an agent
    is constructed at that agent's depth, so its spawned children land at
    depth+1. The registry's depth cap controls whether a nested instance
    is added to an agent's tool set at all.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        master_tools: List[BaseTool],
        provider_getter: ProviderGetter,
        registry: AgentRegistry,
        *,
        parent_depth: int = 0,
        max_batch: int = 5,
        max_iterations: Optional[int] = None,
        master_engagement_store: Optional[EngagementStore] = None,
        temperature: Optional[float] = None,
        max_response_tokens: Optional[int] = None,
    ):
        super().__init__(
            name=SPAWN_TOOL_NAME,
            description=(
                "Delegate a batch of self-contained sub-tasks to isolated "
                "agents. All tasks in the batch run concurrently; you block "
                "until every agent reports back. Use this when the work "
                "branches (e.g. multiple investigation paths) or when a "
                "sub-task would otherwise flood your context with noise."
            ),
        )
        self._agent_manager = agent_manager
        self._master_tools = master_tools
        self._provider_getter = provider_getter
        self._registry = registry
        self._parent_depth = parent_depth
        self._max_batch = max_batch
        self._max_iterations = max_iterations
        self._master_engagement_store = master_engagement_store
        self._temperature = temperature
        self._max_response_tokens = max_response_tokens

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.REQUIRED

    def get_params_class(self) -> Type[ToolParams]:
        return SpawnAgentParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        assert isinstance(toolParams, SpawnAgentParams)
        if len(toolParams.tasks) > self._max_batch:
            return (
                f"Batch size {len(toolParams.tasks)} exceeds AGENT_MAX_BATCH="
                f"{self._max_batch}; split into smaller batches."
            )
        for i, task in enumerate(toolParams.tasks):
            profile = self._agent_manager.get_profile(task.agent_type)
            if profile is None:
                return (
                    f"Task {i}: unknown agent profile '{task.agent_type}'. "
                    "Run with a known profile (see the AGENT PROFILES index)."
                )
            if task.allowed_tools is not None:
                unknown = sorted(set(task.allowed_tools) - set(profile.allowed_tools))
                if unknown:
                    return (
                        f"Task {i}: allowed_tools includes tool(s) not permitted "
                        f"for profile '{task.agent_type}': {', '.join(unknown)}"
                    )
                if not profile.effective_tools(task.allowed_tools):
                    return (
                        f"Task {i}: allowed_tools leaves profile "
                        f"'{task.agent_type}' with no usable tools."
                    )
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        assert isinstance(toolParams, SpawnAgentParams)
        target_depth = self._parent_depth + 1
        task_count = len(toolParams.tasks)

        provider = self._provider_getter()
        if provider is None:
            return ToolResult(
                success=False,
                output="",
                error="No LLM provider is initialized; cannot spawn agents.",
            )

        try:
            self._registry.can_spawn(
                count=task_count, parent_depth=self._parent_depth
            )
        except BudgetExceeded as e:
            return ToolResult(
                success=False,
                output="",
                error=str(e),
            )

        self._registry.record(task_count)

        coros = [
            self._run_one(task, target_depth=target_depth, provider=provider)
            for task in toolParams.tasks
        ]
        # gather with return_exceptions so one crashed agent does not abort
        # the batch — the crash gets turned into a `stuck` report below.
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        reports: List[AgentRunReport] = []
        for i, raw in enumerate(raw_results):
            task = toolParams.tasks[i]
            if isinstance(raw, BaseException):
                logger.error(
                    f"Agent task {i} ({task.agent_type}) raised: {raw!r}"
                )
                reports.append(
                    AgentRunReport(
                        status="stuck",
                        findings=f"Agent crashed: {raw!r}",
                        next_steps="",
                        agent_type=task.agent_type,
                        agent_run_id="-",
                    )
                )
            else:
                reports.append(raw)

        return self._build_tool_result(reports)

    async def _run_one(
        self,
        task: AgentTaskSpec,
        target_depth: int,
        provider: BaseLLMProvider,
    ) -> AgentRunReport:
        """Build and run one `AgentRunner` for a single task spec."""
        profile = self._agent_manager.get_profile(task.agent_type)
        assert profile is not None  # validate_params already checked

        effective_tool_names = profile.effective_tools(task.allowed_tools)
        agent_tools: List[BaseTool] = [
            t for t in self._master_tools if t.name in effective_tool_names
        ]

        # Nested delegation: the agent gets its own `delegate` only when the
        # depth cap permits another level down (see plan §7).
        if not self._registry.depth_cap_reached(target_depth):
            nested_spawn = SpawnAgentTool(
                agent_manager=self._agent_manager,
                master_tools=self._master_tools,
                provider_getter=self._provider_getter,
                registry=self._registry,
                parent_depth=target_depth,
                max_batch=self._max_batch,
                max_iterations=self._max_iterations,
                master_engagement_store=self._master_engagement_store,
                temperature=self._temperature,
                max_response_tokens=self._max_response_tokens,
            )
            agent_tools.append(nested_spawn)

        parent_engagement_id: Optional[str] = None
        if self._master_engagement_store is not None:
            parent_engagement_id = self._master_engagement_store.engagement_id
            agent_store = EngagementStore(store_dir=self._master_engagement_store.store_dir)
        else:
            agent_store = EngagementStore()

        runner = AgentRunner(
            profile=profile,
            instruction=task.instruction,
            success_criteria=task.success_criteria,
            provider=provider,
            tools=agent_tools,
            engagement_store=agent_store,
            parent_engagement_id=parent_engagement_id,
            parent_call_id=self.execution_call_id,
            depth=target_depth,
            max_iterations=self._max_iterations,
            temperature=self._temperature,
            max_response_tokens=self._max_response_tokens,
        )

        return await runner.run()

    @staticmethod
    def _build_tool_result(reports: List[AgentRunReport]) -> ToolResult:
        """Format the agent reports into a single ToolResult for the master."""
        any_stuck = any(r.status == "stuck" for r in reports)
        success = not any_stuck

        sections: List[str] = []
        for i, r in enumerate(reports):
            header = f"### Agent {i + 1} — {r.agent_type} [{r.status}]"
            engagement_line = (
                f"(engagement: {r.engagement_id})" if r.engagement_id else ""
            )
            body = [header, engagement_line.strip(), "", "FINDINGS:", r.findings]
            if r.next_steps:
                body.extend(["", "NEXT STEPS:", r.next_steps])
            sections.append("\n".join(s for s in body if s != ""))
        output = "\n\n".join(sections)

        metadata = {
            "reports": [r.model_dump() for r in reports],
            "any_stuck": any_stuck,
        }
        return ToolResult(
            success=success,
            output=output,
            error=None if success else "One or more agents returned status=stuck",
            metadata=metadata,
        )
