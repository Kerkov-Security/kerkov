"""AgentRunner — runs ONE agent's isolated conversation loop.

Builds a fresh history (profile system prompt + task instruction), a
`LoopSession` wired to a `QuietSink` and `WhitelistAutoApproval`, and
hands it to the shared `ConversationEngine`. The `terminate_after_tool`
hook captures the structured report when the agent calls `report` and
stops the loop.

The runner owns its own `EngagementStore` so the agent's transcript is
persisted to a separate JSONL file linked to the parent's engagement.
"""

import uuid
from typing import List, Literal, Optional

from pydantic import BaseModel

from src.agents.models import AgentProfile
from src.agents.report_tool import REPORT_TOOL_NAME, ReportTool
from src.cli.engagement_store import EngagementStore
from src.core.conversation_engine import (
    ConversationEngine,
    LoopSession,
    QuietSink,
    WhitelistAutoApproval,
)
from src.core.logging.logger_factory import get_logger
from src.models.basic_models import Message, ToolCall
from src.providers.base_provider import BaseLLMProvider
from src.tools.base_tool import BaseTool, ToolResult


logger = get_logger("AgentRunner")


ReportStatus = Literal["completed", "partial", "stuck"]


def _truncate(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        parent_engagement_id: Optional[str] = None,
        parent_call_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        depth: int = 1,
        max_response_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        engine: Optional[ConversationEngine] = None,
    ):
        self.profile = profile
        self.instruction = instruction
        self.success_criteria = success_criteria
        self.provider = provider
        self.tools = tools
        self.engagement_store = engagement_store
        self.parent_engagement_id = parent_engagement_id
        self.parent_call_id = parent_call_id
        self.agent_run_id = agent_run_id or uuid.uuid4().hex[:12]
        self.depth = depth
        self.max_response_tokens = max_response_tokens
        self.temperature = temperature
        self.engine = engine or ConversationEngine()

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
            approval=WhitelistAutoApproval(),
            output=QuietSink(agent_logger=logger),
            max_iterations=self.profile.max_iterations,
            temperature=self.temperature,
            max_response_tokens=self.max_response_tokens,
            engagement_store=self.engagement_store,
            interrupt=None,
            transient_prompts=[],
            terminate_after_tool=capture_and_terminate,
        )

        last_response = await self.engine.run_loop(session)

        return self._build_report(captured_report, last_response, engagement_id, history)

    def _build_initial_history(self) -> List[Message]:
        """Fresh history: the profile's system prompt + the task instruction."""
        system_prompt = self.profile.system_prompt
        user_content = self.instruction
        if self.success_criteria:
            user_content = (
                f"{self.instruction}\n\nSuccess criteria:\n{self.success_criteria}"
            )
        return [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_content),
        ]

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

        if last_response is not None and last_response.content and not last_response.tool_calls:
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
