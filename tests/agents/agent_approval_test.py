"""Tests for how a detached agent's tool calls are approved."""

import asyncio
import unittest
from unittest.mock import MagicMock

from kerkov.agents.agent_approval import AgentReviewGate, SupervisedAgentApproval
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.cli.tool_approval_helper import ToolApprovalResult


def _run(coro):
    return asyncio.run(coro)


def _create_mock_session(agent_id: str = "a1") -> AgentSession:
    session = AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile="recon",
        task="ping 10.0.0.5",
        name="google-pinger",
    )
    session.state = AgentState.RUNNING
    return session


def _create_mock_tool(name: str = "exec") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


class _RecordingGate(AgentReviewGate):
    """Answers every review the same way, recording what it saw."""

    def __init__(self, review: bool, approved: bool = True) -> None:
        self._review = review
        self._approved = approved
        self.asked = []
        self.state_while_asked = None

    def should_review(self, session):
        return self._review

    async def review(self, session, tool, params):
        self.asked.append((session.id, tool.name, params))
        self.state_while_asked = session.state
        return ToolApprovalResult(approved=self._approved)


class SupervisedAgentApprovalTest(unittest.TestCase):
    def test_auto_approves_without_a_gate(self):
        session = _create_mock_session()
        strategy = SupervisedAgentApproval(session)

        result = _run(strategy.approve(_create_mock_tool(), {"command": "id"}))

        self.assertTrue(result.approved)
        self.assertEqual(session.state, AgentState.RUNNING)

    def test_auto_approves_when_the_gate_declines_to_review(self):
        session = _create_mock_session()
        gate = _RecordingGate(review=False)

        result = _run(
            SupervisedAgentApproval(session, gate).approve(
                _create_mock_tool(), {"command": "id"}
            )
        )

        self.assertTrue(result.approved)
        self.assertEqual(gate.asked, [])

    def test_reviewed_call_is_put_to_the_gate(self):
        session = _create_mock_session()
        gate = _RecordingGate(review=True, approved=False)

        result = _run(
            SupervisedAgentApproval(session, gate).approve(
                _create_mock_tool("http"), {"url": "http://10.0.0.5"}
            )
        )

        self.assertFalse(result.approved)
        self.assertEqual(gate.asked, [("a1", "http", {"url": "http://10.0.0.5"})])

    def test_agent_reads_as_blocked_while_the_operator_is_asked(self):
        session = _create_mock_session()
        gate = _RecordingGate(review=True)

        _run(SupervisedAgentApproval(session, gate).approve(_create_mock_tool(), {}))

        self.assertEqual(gate.state_while_asked, AgentState.BLOCKED_ON_INPUT)
        self.assertEqual(session.state, AgentState.RUNNING)

    def test_a_state_change_during_the_prompt_is_not_overwritten(self):
        """A cancel landing while the box is open must not be papered over."""
        session = _create_mock_session()

        class _CancellingGate(_RecordingGate):
            async def review(self, agent_session, tool, params):
                agent_session.set_state(AgentState.CANCELLED)
                return ToolApprovalResult(approved=True)

        _run(
            SupervisedAgentApproval(session, _CancellingGate(review=True)).approve(
                _create_mock_tool(), {}
            )
        )

        self.assertEqual(session.state, AgentState.CANCELLED)


if __name__ == "__main__":
    unittest.main()
