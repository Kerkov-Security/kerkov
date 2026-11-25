"""Tests for how a detached agent's tool calls are approved."""

import asyncio
import unittest
from unittest.mock import MagicMock

from kerkov.agents.agent_approval import (
    AgentDenyRules,
    AgentReviewGate,
    SupervisedAgentApproval,
)
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.cli.tool_approval_helper import ToolApprovalResult
from kerkov.core.session.approval_mode_manager import (
    ApprovalMode,
    ApprovalModeManager,
)
from kerkov.core.session.approval_rules import ApprovalRulesEngine, parse_rules


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


def _create_mock_deny_rules(
    *rules: dict, mode: ApprovalMode = ApprovalMode.NORMAL
) -> AgentDenyRules:
    engine = ApprovalRulesEngine(file_rules=parse_rules(list(rules)))
    return AgentDenyRules(engine, ApprovalModeManager(initial=mode))


_SHIPPED_SUDO_DENY = {"deny": "exec({'command': 're:(^|[^a-z])sudo($|[^a-z])'})"}


class AgentDenyRulesTest(unittest.TestCase):
    """The master's deny rules bind agents; nothing else about approval changes."""

    def test_a_deny_rule_rejects_an_agent_call(self):
        session = _create_mock_session()
        strategy = SupervisedAgentApproval(
            session, deny_rules=_create_mock_deny_rules(_SHIPPED_SUDO_DENY)
        )

        result = _run(strategy.approve(_create_mock_tool(), {"command": "sudo id"}))

        self.assertFalse(result.approved)
        self.assertIn("denied by rule: exec", session.events.buffer)

    def test_a_deny_rule_wins_before_the_operator_is_asked(self):
        session = _create_mock_session()
        gate = _RecordingGate(review=True, approved=True)
        strategy = SupervisedAgentApproval(
            session, gate, _create_mock_deny_rules(_SHIPPED_SUDO_DENY)
        )

        result = _run(strategy.approve(_create_mock_tool(), {"command": "sudo id"}))

        self.assertFalse(result.approved)
        self.assertEqual(gate.asked, [])

    def test_an_unmatched_call_still_auto_approves(self):
        """No prompt: a detached agent has nobody to ask about unmatched calls."""
        strategy = SupervisedAgentApproval(
            _create_mock_session(),
            deny_rules=_create_mock_deny_rules(_SHIPPED_SUDO_DENY),
        )

        result = _run(strategy.approve(_create_mock_tool(), {"command": "id"}))

        self.assertTrue(result.approved)

    def test_yolo_skips_the_rules_as_it_does_for_the_master(self):
        strategy = SupervisedAgentApproval(
            _create_mock_session(),
            deny_rules=_create_mock_deny_rules(
                _SHIPPED_SUDO_DENY, mode=ApprovalMode.YOLO
            ),
        )

        result = _run(strategy.approve(_create_mock_tool(), {"command": "sudo id"}))

        self.assertTrue(result.approved)

    def test_rules_and_mode_are_read_live(self):
        """A rule added or a mode switched mid-run reaches a running agent."""
        engine = ApprovalRulesEngine()
        mode = ApprovalModeManager(initial=ApprovalMode.NORMAL)
        deny_rules = AgentDenyRules(engine, mode)

        self.assertFalse(deny_rules.denies("http", {"url": "http://10.0.0.9"}))
        engine.add_session_rule(
            parse_rules([{"deny": "http({'url': 'http://10.0.0.9*'})"}])[0]
        )
        self.assertTrue(deny_rules.denies("http", {"url": "http://10.0.0.9"}))
        mode.set(ApprovalMode.YOLO)
        self.assertFalse(deny_rules.denies("http", {"url": "http://10.0.0.9"}))


if __name__ == "__main__":
    unittest.main()
