"""How a detached agent's tool calls are approved.

An agent auto-approves inside the whitelist the user signed off on when they
approved the delegation — that is what makes detachment work at all, since
nobody is watching most of the time. `SupervisedAgentApproval` keeps that
default and adds one exception: while the operator is attached and has asked to
review this agent, its calls are put to them like the master's own.

The decision lives behind `AgentReviewGate` because it is a terminal question
("is this agent the one on screen, and can we prompt right now?") that the
agents package has no business answering. `cli/agent_review.py` implements it.
"""

from abc import ABC, abstractmethod
from typing import Optional

from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.conversation_engine import ApprovalStrategy
from kerkov.tools.base_tool import BaseTool


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


class SupervisedAgentApproval(ApprovalStrategy):
    """Whitelist auto-approval, unless the watching operator asked to review."""

    def __init__(
        self,
        session: AgentSession,
        gate: Optional[AgentReviewGate] = None,
    ) -> None:
        self._session = session
        self._gate = gate

    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
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
