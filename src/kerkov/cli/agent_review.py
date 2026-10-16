"""The terminal half of agent tool review: who may ask, and how they ask.

Review is deliberately narrow. An agent only prompts while the operator is
**attached to that agent** and has turned review on for it, because those are
the only conditions under which there is somebody to answer: a detached agent
would block on a question nobody can see, and only one agent is on screen at a
time so two of them can never compete for the answer.

Getting the question onto the screen is the awkward part. The master's chat
prompt normally owns stdin, so the gate takes the terminal from it
(`InputManager.takeover`) for exactly as long as the approval box is up; the
chat prompt is re-drawn afterwards with whatever had been typed into it.
"""

from typing import TYPE_CHECKING

from kerkov.agents.agent_approval import AgentReviewGate
from kerkov.agents.agent_session import AgentSession
from kerkov.cli.input_manager import InputManager
from kerkov.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.session.approval_mode_manager import ApprovalModeManager
from kerkov.tools.base_tool import BaseTool

if TYPE_CHECKING:
    from kerkov.cli.agent_focus_manager import AgentFocusManager
    from kerkov.cli.buffering_sink import BufferingSink

logger = get_logger("AgentReview")


class AttachedAgentReview(AgentReviewGate):
    """Puts the focused agent's tool calls to the operator watching it."""

    def __init__(
        self,
        focus: "AgentFocusManager",
        input_manager: InputManager,
        helper: ToolApprovalCliHelper,
        approval_mode: ApprovalModeManager,
        output_sink: "BufferingSink",
    ) -> None:
        self._focus = focus
        self._input = input_manager
        self._helper = helper
        self._approval_mode = approval_mode
        self._output = output_sink

    def should_review(self, session: AgentSession) -> bool:
        if not session.review_tools:
            return False
        # A one-shot `--message` run auto-approves everything and has no
        # terminal to prompt on; asking there would hang the invocation.
        if self._helper.non_interactive_auto_approve:
            return False
        return self._focus.agent_id == session.id

    async def review(
        self, session: AgentSession, tool: BaseTool, params: dict
    ) -> ToolApprovalResult:
        session.events.info(f"waiting for approval: {tool.name}")
        async with self._input.takeover():
            # Waiting for an open picker can take arbitrarily long. Focus or
            # review mode may have changed while it owned the terminal.
            if not self.should_review(session):
                return ToolApprovalResult(approved=True)
            with self._output.suspend_status():
                result = await self._helper.get_tool_usage_approval(
                    tool,
                    params,
                    self._approval_mode.current,
                    requester=session.display_name(),
                )
        session.events.info(
            f"{'approved' if result.approved else 'denied'}: {tool.name}"
        )
        logger.info(
            f"Agent {session.id} tool '{tool.name}' reviewed: "
            f"approved={result.approved}"
        )
        return result
