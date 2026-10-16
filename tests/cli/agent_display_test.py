"""Tests for the shared agent renderings — chips, labels, and the prompt strip."""

import time
import types
import unittest
from unittest.mock import MagicMock

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.cli.agent_display import (
    MASTER_CHIP,
    REVIEW_MARKER,
    agent_chip,
    agent_choice_label,
    agent_strip,
)
from kerkov.cli.agent_focus_manager import MASTER_FOCUS, AgentFocusManager
from kerkov.cli.kerkov_cli import KerkovCLI


def _create_mock_session(
    agent_id: str = "a1",
    name: str = "google-pinger",
    state: AgentState = AgentState.RUNNING,
) -> AgentSession:
    session = AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile="recon",
        task="ping 10.0.0.5",
        name=name,
    )
    session.state = state
    session.started_at = time.monotonic()
    if session.is_finished:
        session.finished_at = session.started_at + 42.0
    return session


def _create_mock_cli(*sessions: AgentSession) -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.agent_focus = AgentFocusManager()
    cli.agent_supervisor = AgentSupervisor(AgentRegistry(max_depth=2, max_total=20))
    for session in sessions:
        cli.agent_supervisor._sessions[session.id] = session
    return cli


def _text(fragments) -> str:
    return "".join(text for _, text in fragments)


class AgentChipTest(unittest.TestCase):

    def test_chip_names_the_agent_and_what_it_is_doing(self):
        session = _create_mock_session()
        session.current_tool = "exec"

        self.assertEqual(agent_chip(session), "● google-pinger exec")

    def test_a_finished_agent_carries_the_finished_marker(self):
        session = _create_mock_session(state=AgentState.CANCELLED)

        self.assertTrue(agent_chip(session).startswith("○"))

    def test_a_reviewed_agent_says_so(self):
        session = _create_mock_session()
        session.review_tools = True

        self.assertIn(REVIEW_MARKER, agent_chip(session))


class AgentChoiceLabelTest(unittest.TestCase):

    def test_picker_shows_selected_model_tier_and_reason_as_plain_text(self):
        session = _create_mock_session()
        session.provider = "anthropic"
        session.model = "small[model]"
        session.routing_tier = "small"
        session.routing_reason = "[bold]Explicit extraction[/bold]"

        label = agent_choice_label(session)

        self.assertIn("anthropic / small[model] (small)", label)
        self.assertIn("[bold]Explicit extraction[/bold]", label)

    def test_older_session_without_routing_info_keeps_its_status(self):
        label = agent_choice_label(_create_mock_session())

        self.assertIn("thinking", label)
        self.assertNotIn("None", label)


class AgentStripTest(unittest.TestCase):

    def test_master_is_always_the_first_chip(self):
        strip = agent_strip([_create_mock_session()])

        self.assertTrue(_text(strip).startswith(MASTER_CHIP))

    def test_the_focused_entry_is_styled_and_the_others_are_not(self):
        sessions = [_create_mock_session("a1"), _create_mock_session("a2", "parser")]

        strip = agent_strip(sessions, focused="a2")

        styled = [text for style, text in strip if style]
        self.assertEqual(len(styled), 1)
        self.assertIn("parser", styled[0])

    def test_master_is_the_styled_entry_when_nothing_is_attached(self):
        strip = agent_strip([_create_mock_session()], focused=MASTER_FOCUS)

        styled = [text for style, text in strip if style]
        self.assertEqual(styled, [MASTER_CHIP])

    def test_overflow_is_counted_rather_than_wrapped(self):
        sessions = [_create_mock_session(f"a{i}", f"agent-{i}") for i in range(6)]

        strip = agent_strip(sessions, max_chips=2)

        text = _text(strip)
        self.assertIn("agent-1", text)
        self.assertNotIn("agent-5", text)
        self.assertIn("+4 more", text)

    def test_no_agents_leaves_only_the_master_chip(self):
        self.assertEqual(_text(agent_strip([])), MASTER_CHIP)

    def test_an_agent_name_can_never_be_read_as_styling(self):
        """Fragments, not markup: the model chooses the name."""
        session = _create_mock_session(name="<b>evil</b>")

        strip = agent_strip([session])

        self.assertIn("<b>evil</b>", _text(strip))


class CliStatusStripTest(unittest.TestCase):

    def test_no_agents_means_no_strip(self):
        cli = _create_mock_cli()

        self.assertIsNone(cli._agent_status_strip())

    def test_a_running_agent_appears(self):
        cli = _create_mock_cli(_create_mock_session("a1"))

        self.assertIn("google-pinger", _text(cli._agent_status_strip()))

    def test_a_finished_agent_alone_does_not_hold_the_strip_up(self):
        cli = _create_mock_cli(_create_mock_session("a1", state=AgentState.DONE))

        self.assertIsNone(cli._agent_status_strip())

    def test_a_finished_agent_rides_along_beside_a_running_one(self):
        done = _create_mock_session("a1", "log-parser", state=AgentState.DONE)
        cli = _create_mock_cli(done, _create_mock_session("a2", "port-scan"))

        self.assertIn("log-parser", _text(cli._agent_status_strip()))

    def test_running_agents_come_before_finished_ones(self):
        done = _create_mock_session("a1", "log-parser", state=AgentState.DONE)
        running = _create_mock_session("a2", "port-scan")
        cli = _create_mock_cli(done, running)

        text = _text(cli._agent_status_strip())

        self.assertLess(text.index("port-scan"), text.index("log-parser"))

    def test_the_strip_marks_the_attached_agent(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        styled = [text for style, text in cli._agent_status_strip() if style]

        self.assertEqual(len(styled), 1)
        self.assertIn("google-pinger", styled[0])


if __name__ == "__main__":
    unittest.main()
