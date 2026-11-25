"""How a detached agent's tool calls are approved.

An agent auto-approves inside the whitelist the user signed off on when they
approved the delegation — that is what makes detachment work at all, since
nobody is watching most of the time. `SupervisedAgentApproval` keeps that
default and adds one exception: while the operator is attached and has asked to
review this agent, its calls are put to them like the master's own.

The decision lives behind `AgentReviewGate` because it is a terminal question
("is this agent the one on screen, and can we prompt right now?") that the
agents package has no business answering. `cli/agent_review.py` implements it.

The operator's deny rules apply before either, exactly as they do to the master:
a whitelist decides what an agent may run without asking, not what it may run
that the operator has forbidden outright.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.conversation_engine import ApprovalStrategy
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.session.approval_mode_manager import (
    ApprovalMode,
    ApprovalModeManager,
)
from kerkov.core.session.approval_rules import ApprovalRulesEngine
from kerkov.tools.base_tool import BaseTool

logger = get_logger("AgentApproval")


class AgentReviewGate(ABC):
    """Whether — and how — one agent's tool call is put to the operator."""

    @abstractmethod
    def should_review(self, session: AgentSession) -> bool:
        """True when this call must wait for an answer instead of auto-approving."""

    @abstractmethod
    async def review(
        self, session: AgentSession, tool: BaseTool, params: dict
    ) -> ToolApprovalResult:
        """Ask the operator about one call."""


class AgentDenyRules:
    """The master's deny rules, read live and applied to every agent's calls.

    Only `deny` is honoured: allow rules and unmatched calls stay with the
    whitelist, because a detached agent has nobody to prompt. The engine and
    mode are the front end's own, so a rule saved or a `/yolo` switched mid-run
    reaches agents already running.
    """

    def __init__(
        self, rules_engine: ApprovalRulesEngine, mode_manager: ApprovalModeManager
    ) -> None:
        self._rules_engine = rules_engine
        self._mode_manager = mode_manager

    def denies(self, tool_name: str, params: Dict[str, Any]) -> bool:
        if self._mode_manager.current == ApprovalMode.YOLO:
            return False
        return self._rules_engine.evaluate(tool_name, params) == "deny"


class SupervisedAgentApproval(ApprovalStrategy):
    """Deny rules first, then whitelist auto-approval or the operator's review."""

    def __init__(
        self,
        session: AgentSession,
        gate: Optional[AgentReviewGate] = None,
        deny_rules: Optional[AgentDenyRules] = None,
    ) -> None:
        self._session = session
        self._gate = gate
        self._deny_rules = deny_rules

    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
        if self._deny_rules is not None and self._deny_rules.denies(tool.name, params):
            self._session.events.info(f"denied by rule: {tool.name}")
            logger.info(f"Agent {self._session.id} tool '{tool.name}' denied by rule")
            return ToolApprovalResult(approved=False)

        if self._gate is None or not self._gate.should_review(self._session):
            return ToolApprovalResult(approved=True)

        # The state is restored rather than forced back to RUNNING: a cancel
        # landing while the box is open must not be papered over.
        previous = self._session.state
        self._session.set_state(AgentState.BLOCKED_ON_INPUT)
        try:
            return await self._gate.review(self._session, tool, params)
        finally:
            if self._session.state == AgentState.BLOCKED_ON_INPUT:
                self._session.set_state(previous)
