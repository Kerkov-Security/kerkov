"""SpawnAgentTool — the master delegates a batch of self-contained tasks.

One tool call carries N task specs and detaches them: the agents run as
supervised background tasks and the call returns immediately, so the master
keeps its prompt. Reports come back either through `agent_wait` or, if the
master never asks, through the supervisor's turn-boundary delivery.

Batching still matters — the conversation engine executes a turn's tool calls
serially, so N separate spawn calls would stagger the launches — but the fan-out
itself now outlives the call.

Detachment is the master's alone (`detached=True`). The nested instance handed
to an agent blocks on its children instead: an agent has no prompt to keep
responsive and no turn boundary to deliver into, so detaching there would let it
report without its children's findings while those findings surfaced in the
master's conversation.

This tool owns *how* to build an agent (profile → tool set → nested delegate
→ engagement store); `AgentSupervisor` owns its lifecycle. The two meet at
`AgentSpawnSpec.build_runner`.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, Field

from kerkov.agents.agent_approval import AgentReviewGate, SupervisedAgentApproval
from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry, BudgetExceeded
from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.report_format import build_report_result
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.logging.logger_factory import get_logger
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.routing import RoutingDecision, RoutingPolicy, TaskRoutingContext
from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)

ProviderGetter = Callable[[], Optional[BaseLLMProvider]]
KnowledgeBaseGetter = Callable[[], Optional[EngagementKnowledgeBase]]
RoutingGetter = Callable[[], Optional[RoutingPolicy]]


logger = get_logger("SpawnAgentTool")


SPAWN_TOOL_NAME = "delegate"

_SHARED_DESCRIPTION = (
    "Use this when the work branches (e.g. multiple investigation paths) or "
    "when a sub-task would otherwise flood your context with noise."
)

_DETACHED_DESCRIPTION = (
    "Delegate a batch of self-contained sub-tasks to isolated agents. The "
    "agents run in the background and this call returns immediately with "
    "their ids — collect their reports with `agent_wait`, or keep working "
    "and they will be delivered to you when they finish. " + _SHARED_DESCRIPTION
)

_BLOCKING_DESCRIPTION = (
    "Delegate a batch of self-contained sub-tasks to isolated agents. The "
    "agents run concurrently and this call returns their reports once they "
    "have all finished. " + _SHARED_DESCRIPTION
)


class AgentTaskSpec(BaseModel):
    """One delegated task in a `delegate` batch."""

    agent_type: str = Field(
        description="Profile name: built-in (recon, analysis, exploit, general) "
        "or a user-defined AGENT.md."
    )
    instruction: str = Field(description="Clear, self-contained task for the agent.")
    name: str = Field(
        default="",
        description="Short kebab-case name for this task, 2-4 words, describing "
        "what the agent is doing (e.g. 'google-pinger', 'tomcat-enum'). This is "
        "what the user sees while it runs, so make it recognisable.",
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
    task_kind: Literal["general", "extract", "format", "analyze"] = Field(
        default="general",
        description="Routing hint. Use extract or format only for routine extraction "
        "or reformatting of supplied facts (small tier), or analyze for bounded "
        "routine analysis (medium tier). In auto mode only the built-in analysis "
        "profile with tools restricted to read/recall and bounded, nonempty success criteria may "
        "use small or medium models; these agents cannot delegate. Leave general "
        "for complex, sensitive, or uncertain work (large tier).",
    )


class SpawnAgentParams(ToolParams):
    """Batch payload for `delegate`."""

    tasks: List[AgentTaskSpec] = Field(
        description="One or more tasks to delegate. All are launched "
        "concurrently and run in the background; this call returns as soon "
        "as they start.",
        min_length=1,
    )


class SpawnAgentTool(BaseTool):
    """Delegate one or more isolated sub-tasks to agents.

    Built fresh on each construction with the caller's depth: master
    constructs at `parent_depth=0`; a nested instance handed to an agent
    is constructed at that agent's depth, so its spawned children land at
    depth+1. The registry's depth cap controls whether a nested instance
    is added to an agent's tool set at all.

    `detached` is the master's mode only — a nested instance blocks, because
    an agent has no way to collect a detached child (`agent_wait` is a master
    tool) and would otherwise report without its children's findings.
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        master_tools: List[BaseTool],
        provider_getter: ProviderGetter,
        registry: AgentRegistry,
        supervisor: AgentSupervisor,
        *,
        parent_depth: int = 0,
        detached: bool = True,
        max_batch: int = 5,
        max_iterations: Optional[int] = None,
        master_engagement_store: Optional[EngagementStore] = None,
        knowledge_base_getter: Optional[KnowledgeBaseGetter] = None,
        temperature: Optional[float] = None,
        max_response_tokens: Optional[int] = None,
        review_gate: Optional[AgentReviewGate] = None,
        routing_getter: Optional[RoutingGetter] = None,
    ):
        super().__init__(
            name=SPAWN_TOOL_NAME,
            description=_DETACHED_DESCRIPTION if detached else _BLOCKING_DESCRIPTION,
        )
        self._agent_manager = agent_manager
        self._master_tools = master_tools
        self._provider_getter = provider_getter
        self._registry = registry
        self._supervisor = supervisor
        self._parent_depth = parent_depth
        self._detached = detached
        self._max_batch = max_batch
        self._max_iterations = max_iterations
        self._master_engagement_store = master_engagement_store
        # Agents write into the *master's* knowledge base rather than one of
        # their own, so their enumeration is recallable by the master (and by
        # a later batch) instead of being sealed in a per-agent file.
        self._knowledge_base_getter = knowledge_base_getter
        self._temperature = temperature
        self._max_response_tokens = max_response_tokens
        # Lets an attached operator review this agent's calls; None keeps the
        # whitelist auto-approval agents run with by default.
        self._review_gate = review_gate
        self._routing_getter = routing_getter

    def get_params_class(self) -> Type[ToolParams]:
        return SpawnAgentParams

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        """Offer no broad rung: each batch is a distinct grant of autonomy.

        Spawned agents auto-approve inside their whitelist, so approval of one
        batch must not carry to the next.
        """
        return ApprovalScope.exact_only()

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

        provider = self._provider_getter()
        if provider is None:
            return ToolResult(
                success=False,
                output="",
                error="No LLM provider is initialized; cannot spawn agents.",
            )

        # Snapshot policy once for the batch. A running parent's children keep
        # this policy even if the operator changes the main conversation later.
        try:
            routing_policy = self._routing_getter() if self._routing_getter else None
        except ValueError as exc:
            return ToolResult(success=False, output="", error=f"Model routing: {exc}")

        specs = [
            AgentSpawnSpec(
                profile=task.agent_type,
                task=task.instruction,
                name=task.name,
                build_runner=self._runner_factory(
                    task,
                    target_depth=target_depth,
                    provider=provider,
                    routing_policy=routing_policy,
                ),
            )
            for task in toolParams.tasks
        ]

        try:
            sessions = self._supervisor.spawn(specs, parent_depth=self._parent_depth)
        except BudgetExceeded as e:
            return ToolResult(success=False, output="", error=str(e))

        if self._detached:
            return self._detached_result(sessions)
        return await self._collect(sessions)

    async def _collect(self, sessions: List[AgentSession]) -> ToolResult:
        """Block until this batch reports, then hand the caller its findings.

        Scoped to the ids just spawned: an unscoped wait would let one agent
        consume its siblings' and the master's undelivered reports.
        """
        ids = [s.id for s in sessions]
        try:
            outcome = await self._supervisor.wait(ids=ids)
        except asyncio.CancelledError:
            # A cancelled parent must not leave its children running with
            # nobody left to collect them.
            for agent_id in ids:
                self._supervisor.request_cancel(agent_id)
            raise
        return build_report_result(
            outcome.reports,
            still_running=outcome.pending,
            unknown=outcome.unknown,
        )

    def _runner_factory(
        self,
        task: AgentTaskSpec,
        *,
        target_depth: int,
        provider: BaseLLMProvider,
        routing_policy: Optional[RoutingPolicy] = None,
    ) -> Callable[[AgentSession, Optional[Path]], AgentRunner]:
        """Close over one task spec; the supervisor calls this to build its runner."""

        def build(session: AgentSession, workspace: Optional[Path]) -> AgentRunner:
            profile = self._agent_manager.get_profile(task.agent_type)
            assert profile is not None  # validate_params already checked
            decision = (
                routing_policy.select(
                    TaskRoutingContext(
                        task_kind=task.task_kind,
                        profile=profile.name,
                        instruction=task.instruction,
                        success_criteria=task.success_criteria,
                        tools=tuple(profile.effective_tools(task.allowed_tools)),
                        trusted_profile=profile.source == "builtin",
                    )
                )
                if routing_policy is not None
                else RoutingDecision(
                    model=provider.model,
                    tier="manual",
                    reason="Using the selected conversation model.",
                )
            )
            # Provider construction stays within the supervisor's per-agent
            # failure boundary and after budget checks. Never mutate the
            # conversation provider, including when auto mode is disabled.
            session.provider = provider.__class__.__name__
            session.model = decision.model
            session.routing_tier = decision.tier
            session.routing_reason = decision.reason
            selected_provider = provider.fork(
                decision.model if routing_policy is not None else None
            )
            return self._build_runner(
                task,
                target_depth=target_depth,
                provider=selected_provider,
                session=session,
                workspace=workspace,
                routing_policy=routing_policy,
                routing_decision=decision,
            )

        return build

    def _build_runner(
        self,
        task: AgentTaskSpec,
        *,
        target_depth: int,
        provider: BaseLLMProvider,
        session: AgentSession,
        workspace: Optional[Path],
        routing_policy: Optional[RoutingPolicy] = None,
        routing_decision: Optional[RoutingDecision] = None,
    ) -> AgentRunner:
        """Build the `AgentRunner` for a single task spec."""
        profile = self._agent_manager.get_profile(task.agent_type)
        assert profile is not None  # validate_params already checked

        effective_tool_names = profile.effective_tools(task.allowed_tools)
        agent_tools: List[BaseTool] = [
            t for t in self._master_tools if t.name in effective_tool_names
        ]

        # Restricted small/medium tasks must not use delegation to obtain the
        # master's wider tool pool. Large/manual tasks retain nested delegation.
        restricted_task = routing_decision is not None and routing_decision.tier in {
            "small",
            "medium",
        }
        if not restricted_task and not self._registry.depth_cap_reached(target_depth):
            nested_spawn = SpawnAgentTool(
                agent_manager=self._agent_manager,
                master_tools=self._master_tools,
                provider_getter=lambda: provider,
                registry=self._registry,
                supervisor=self._supervisor,
                parent_depth=target_depth,
                detached=False,
                max_batch=self._max_batch,
                max_iterations=self._max_iterations,
                master_engagement_store=self._master_engagement_store,
                knowledge_base_getter=self._knowledge_base_getter,
                temperature=self._temperature,
                max_response_tokens=self._max_response_tokens,
                review_gate=self._review_gate,
                routing_getter=lambda: routing_policy,
            )
            agent_tools.append(nested_spawn)

        parent_engagement_id: Optional[str] = None
        if self._master_engagement_store is not None:
            parent_engagement_id = self._master_engagement_store.engagement_id
            agent_store = EngagementStore(
                store_dir=self._master_engagement_store.store_dir
            )
        else:
            agent_store = EngagementStore()

        return AgentRunner(
            profile=profile,
            instruction=task.instruction,
            success_criteria=task.success_criteria,
            provider=provider,
            tools=agent_tools,
            engagement_store=agent_store,
            knowledge_base=(
                self._knowledge_base_getter()
                if self._knowledge_base_getter is not None
                else None
            ),
            parent_engagement_id=parent_engagement_id,
            parent_call_id=self.execution_call_id,
            depth=target_depth,
            max_iterations=self._max_iterations,
            temperature=self._temperature,
            max_response_tokens=self._max_response_tokens,
            session=session,
            run_workspace=workspace,
            approval=SupervisedAgentApproval(session, self._review_gate),
        )

    @staticmethod
    def _detached_result(sessions: List[AgentSession]) -> ToolResult:
        """Announce the launched agents and state the collection step explicitly.

        The next step is spelled out because the contract changed under the
        model: a batch that used to come back with reports now comes back with
        ids, and a master that does not know that will just move on.
        """
        lines = [
            f"Spawned {len(sessions)} agent(s) (detached). They run in the "
            "background while you keep working.",
        ]
        for session in sessions:
            lines.append(f"- {session.label()}: {session.task}")
            if session.model:
                lines.append(
                    f"  Model: {session.provider} / {session.model} "
                    f"[{session.routing_tier}] — {session.routing_reason}"
                )
        ids = ", ".join(f'"{s.id}"' for s in sessions)
        lines.append(
            f"Call agent_wait(ids=[{ids}]) to block until they report, or "
            "continue working — finished reports are delivered to you "
            "automatically at your next turn."
        )
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "agent_ids": [s.id for s in sessions],
                "detached": True,
                "agents": [
                    {
                        "id": s.id,
                        "name": s.display_name(),
                        "profile": s.profile,
                        "task": s.task,
                        "provider": s.provider,
                        "model": s.model,
                        "routing_tier": s.routing_tier,
                        "routing_reason": s.routing_reason,
                    }
                    for s in sessions
                ],
            },
        )
