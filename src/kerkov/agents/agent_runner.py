"""AgentRunner — runs ONE agent's isolated conversation loop.

Builds a fresh history (profile system prompt + task instruction), a
`LoopSession` wired to a `QuietSink` and an approval strategy (whitelist
auto-approval unless the caller supplies one that can escalate), and
hands it to the shared `ConversationEngine`. The `terminate_after_tool`
hook captures the structured report when the agent calls `report` and
stops the loop.

The runner owns its own `EngagementStore` so the agent's transcript is
persisted to a separate JSONL file linked to the parent's engagement.

When the supervisor hands it an `AgentSession`, the runner writes through to
it: the session's sink narrates the run, the session's list is the live
history an attached view renders, and the agent's engagement id lands there as
soon as the transcript exists.
"""

import uuid
from pathlib import Path
from typing import List, Optional

from kerkov.agents.agent_session import AgentSession
from kerkov.agents.models import AgentProfile, AgentRunReport, ReportStatus
from kerkov.agents.report_tool import REPORT_TOOL_NAME, ReportTool
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.config_manager import get_config
from kerkov.core.conversation_engine import (
    ApprovalStrategy,
    ConversationEngine,
    LoopSession,
    OutputSink,
    QuietSink,
    WhitelistAutoApproval,
)
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    ensure_workspace_dirs,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.models.basic_models import Message, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool, ToolResult

logger = get_logger("AgentRunner")

__all__ = ["AgentRunner", "AgentRunReport", "ReportStatus"]


def _truncate(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AgentRunner:
    """One isolated agent conversation, fronted by `ConversationEngine`."""

    def __init__(
        self,
        profile: AgentProfile,
        instruction: str,
        provider: BaseLLMProvider,
        tools: List[BaseTool],
        *,
        success_criteria: str = "",
        engagement_store: Optional[EngagementStore] = None,
        knowledge_base: Optional[EngagementKnowledgeBase] = None,
        parent_engagement_id: Optional[str] = None,
        parent_call_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        depth: int = 1,
        max_iterations: Optional[int] = None,
        max_response_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        engine: Optional[ConversationEngine] = None,
        session: Optional[AgentSession] = None,
        run_workspace: Optional[Path] = None,
        approval: Optional[ApprovalStrategy] = None,
    ):
        self.profile = profile
        self.instruction = instruction
        self.success_criteria = success_criteria
        self.provider = provider
        self.tools = tools
        self.engagement_store = engagement_store
        # The master's store, shared — not a private one keyed by this agent's
        # own engagement id, which the master could never read back.
        self.knowledge_base = knowledge_base
        # The ledger wraps the same shared store, so the agent reads the tree's
        # confirmed facts (injected each turn) and its writes land there too.
        self._findings_ledger = (
            FindingsLedger(knowledge_base) if knowledge_base is not None else None
        )
        self.parent_engagement_id = parent_engagement_id
        self.parent_call_id = parent_call_id
        self.agent_run_id = agent_run_id or uuid.uuid4().hex[:12]
        self.depth = depth
        self.max_iterations = max_iterations
        self.max_response_tokens = max_response_tokens
        self.temperature = temperature
        self.engine = engine or ConversationEngine()
        self.session = session
        # Injected by the supervisor: the engagement *tree's* shared workspace,
        # not one derived from this agent's own id (which nothing else can see).
        self.run_workspace = run_workspace
        # Whitelist auto-approval unless the caller supplied something that can
        # escalate — an agent with nobody watching has no one to ask.
        self.approval = approval or WhitelistAutoApproval()
        self.output: OutputSink = (
            session.events if session is not None else QuietSink(agent_logger=logger)
        )

    async def run(self) -> AgentRunReport:
        """Run the agent loop to completion, return a structured report."""
        history = self._build_initial_history()
        tools = list(self.tools) + [ReportTool()]

        engagement_id: Optional[str] = None
        if self.engagement_store is not None:
            engagement_id = self.engagement_store.start_engagement(
                mode="agent",
                model=self.provider.model,
                verbosity="silent",
                approval_mode="auto",
                kind="agent",
                parent_engagement_id=self.parent_engagement_id,
                parent_call_id=self.parent_call_id,
                agent_type=self.profile.name,
                task_instruction=_truncate(self.instruction, limit=200),
            )
            for msg in history:
                self.engagement_store.append_message(msg)
            if self.session is not None:
                self.session.engagement_id = engagement_id

        workspace_path = self.run_workspace
        if workspace_path is None and engagement_id is not None:
            workspace_root = get_config().workspace_dir
            workspace_path = RunWorkspaceManager(workspace_root).path_for_engagement(
                engagement_id
            )
        workspace_token = None
        if workspace_path is not None:
            ensure_workspace_dirs(workspace_path)
            workspace_token = set_current_workspace(workspace_path)

        captured_report: dict = {}

        def capture_and_terminate(call: ToolCall, result: ToolResult) -> bool:
            if call.tool_name != REPORT_TOOL_NAME:
                return False
            # The structured fields live on the ToolCall — the LLM populated
            # them. The result's `metadata` mirrors them, but the call is the
            # authoritative source.
            captured_report.update(call.parameters)
            return True

        session = LoopSession(
            provider=self.provider,
            history=history,
            tools=tools,
            approval=self.approval,
            output=self.output,
            max_iterations=self.effective_max_iterations(),
            temperature=self.temperature,
            max_response_tokens=self.max_response_tokens,
            engagement_store=self.engagement_store,
            knowledge_base=self.knowledge_base,
            ledger=self._findings_ledger,
            kb_agent_type=self.profile.name,
            kb_clip_threshold_bytes=get_config().kb_clip_threshold_bytes,
            interrupt=self.session.interrupt if self.session is not None else None,
            # An agent has no operator to say "keep going" — an interrupt is a
            # stop, not the master's "abandon this tool but finish the turn".
            interrupt_ends_turn=self.session is not None,
            inbox=self.session.inbox if self.session is not None else None,
            stop_exempt_tools=frozenset({REPORT_TOOL_NAME}),
            transient_prompts=[],
            terminate_after_tool=capture_and_terminate,
        )

        try:
            last_response = await self.engine.run_loop(session)
        finally:
            if workspace_token is not None:
                reset_current_workspace(workspace_token)

        # A cancel racing a `report` call loses: the agent's own findings beat
        # anything reconstructed from history.
        if not captured_report and self._was_stopped():
            return self._build_stopped_report(engagement_id, history)
        return self._build_report(
            captured_report, last_response, engagement_id, history
        )

    def _was_stopped(self) -> bool:
        """True when the loop ended because the supervisor set the interrupt."""
        return (
            self.session is not None
            and self.session.interrupt.interrupt_requested.is_set()
        )

    def _build_stopped_report(
        self, engagement_id: Optional[str], history: List[Message]
    ) -> AgentRunReport:
        """What the agent had when it was stopped — `partial`, never `stuck`.

        `stuck` reads as "the agent failed"; this run was cut short by the
        operator, and whatever it already found is still worth reporting.
        """
        iterations = self.session.iteration if self.session is not None else 0
        last_text = ""
        for msg in reversed(history):
            if msg.role == "assistant" and msg.content:
                last_text = msg.content
                break
        stopped = f"Stopped by the operator after {iterations} iteration(s)."
        return AgentRunReport(
            status="partial",
            findings=f"{last_text}\n\n{stopped}".strip() if last_text else stopped,
            next_steps="",
            agent_type=self.profile.name,
            agent_run_id=self.agent_run_id,
            engagement_id=engagement_id,
        )

    def _build_initial_history(self) -> List[Message]:
        """Fresh history: the profile's system prompt + the task instruction.

        Filled into the session's list when there is one, so an attached view
        reads the same object the loop is appending to.
        """
        user_content = self.instruction
        if self.success_criteria:
            user_content = (
                f"{self.instruction}\n\nSuccess criteria:\n{self.success_criteria}"
            )
        history = self.session.history if self.session is not None else []
        history.extend(
            [
                Message(role="system", content=self.profile.system_prompt),
                Message(role="user", content=user_content),
            ]
        )
        return history

    def effective_max_iterations(self) -> int:
        """Apply the process-wide agent iteration cap to the profile limit."""
        if self.max_iterations is None:
            return self.profile.max_iterations
        return min(self.profile.max_iterations, self.max_iterations)

    def _build_report(
        self,
        captured: dict,
        last_response,
        engagement_id: Optional[str],
        history: List[Message],
    ) -> AgentRunReport:
        """Map the engine's exit state onto a structured `AgentRunReport`.

        Three paths:
        1. The agent called `report` → use the captured payload.
        2. The agent stopped emitting tool calls without calling `report`
           → wrap its final text as `status="completed"`.
        3. Anything else (max_iterations, no last response) → `stuck`.
        """
        if captured:
            return AgentRunReport(
                status=captured.get("status", "stuck"),
                findings=captured.get("findings", ""),
                next_steps=captured.get("next_steps", ""),
                agent_type=self.profile.name,
                agent_run_id=self.agent_run_id,
                engagement_id=engagement_id,
            )

        if (
            last_response is not None
            and last_response.content
            and not last_response.tool_calls
        ):
            return AgentRunReport(
                status="completed",
                findings=last_response.content,
                next_steps="",
                agent_type=self.profile.name,
                agent_run_id=self.agent_run_id,
                engagement_id=engagement_id,
            )

        last_text = ""
        for msg in reversed(history):
            if msg.role == "assistant" and msg.content:
                last_text = msg.content
                break

        return AgentRunReport(
            status="stuck",
            findings=last_text or "Agent stopped without calling report.",
            next_steps="",
            agent_type=self.profile.name,
            agent_run_id=self.agent_run_id,
            engagement_id=engagement_id,
        )
