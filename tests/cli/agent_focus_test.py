"""Tests for agent focus: routing, prompt prefix, and the `/agents` picker."""

import asyncio
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.cli.agent_focus_manager import MASTER_FOCUS, AgentFocusManager
from kerkov.cli.input_parser import InputParser
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.messages.agents_message import AgentsMessage, BackMessage
from kerkov.cli.messages.general_message import GeneralMessage


def _run(coro):
    return asyncio.run(coro)


def _create_mock_chooser(selection):
    """Stand in for `InputManager.choose`, recording the options it was given."""

    async def _choose(*args, **kwargs):
        return selection

    return MagicMock(side_effect=_choose)


def _create_mock_session(
    agent_id: str = "a1",
    name: str = "google-pinger",
    profile: str = "recon",
    state: AgentState = AgentState.RUNNING,
) -> AgentSession:
    session = AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile=profile,
        task="ping 10.0.0.5",
        name=name,
    )
    session.state = state
    session.started_at = time.monotonic()
    session.max_iterations = 30
    session.iteration = 6
    if session.is_finished:
        session.finished_at = session.started_at + 42.0
    return session


def _create_mock_cli(*sessions: AgentSession) -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.agent_focus = AgentFocusManager()
    cli.agent_supervisor = AgentSupervisor(AgentRegistry(max_depth=2, max_total=20))
    for session in sessions:
        cli.agent_supervisor._sessions[session.id] = session
    cli.agent_attach = MagicMock()
    cli.input_manager = MagicMock()
    cli.mode_manager = MagicMock()
    # `name` is reserved on MagicMock, and the prompt prefix reads `mode.name[0]`.
    cli.mode_manager.current_mode = types.SimpleNamespace(color="green", name="AUDIT")
    return cli


class AgentFocusManagerTest(unittest.TestCase):

    def test_starts_on_master(self):
        focus = AgentFocusManager()

        self.assertTrue(focus.is_master)
        self.assertIsNone(focus.agent_id)
        self.assertEqual(focus.current, MASTER_FOCUS)

    def test_focus_agent_then_master(self):
        focus = AgentFocusManager()

        focus.focus_agent("a2")
        self.assertFalse(focus.is_master)
        self.assertEqual(focus.current, "a2")

        focus.focus_master()
        self.assertTrue(focus.is_master)
        self.assertEqual(focus.current, MASTER_FOCUS)


class AgentsCommandParsingTest(unittest.TestCase):

    def test_bare_agents_opens_picker(self):
        message = InputParser.parse_for_message_type("/agents")

        self.assertIsInstance(message, AgentsMessage)
        self.assertIsNone(message.target)

    def test_agents_with_target_attaches_directly(self):
        message = InputParser.parse_for_message_type("/agents google-pinger")

        self.assertIsInstance(message, AgentsMessage)
        self.assertEqual(message.target, "google-pinger")

    def test_back_is_registered(self):
        self.assertIsInstance(InputParser.parse_for_message_type("/back"), BackMessage)


@patch("kerkov.cli.messages.agents_message.console", MagicMock())
class AgentsPickerTest(unittest.TestCase):

    def test_picker_lists_master_and_every_agent(self):
        cli = _create_mock_cli(
            _create_mock_session("a1", "google-pinger"),
            _create_mock_session("a2", "log-parser", state=AgentState.DONE),
        )
        cli.input_manager.choose = _create_mock_chooser(None)

        _run(AgentsMessage().execute(cli))

        options = cli.input_manager.choose.call_args.args[1]
        self.assertEqual([value for value, _ in options], [MASTER_FOCUS, "a1", "a2"])

    def test_picker_row_carries_profile_task_state_and_progress(self):
        cli = _create_mock_cli(_create_mock_session("a1", "google-pinger"))
        cli.agent_supervisor.get("a1").current_tool = "exec"
        cli.input_manager.choose = _create_mock_chooser(None)

        _run(AgentsMessage().execute(cli))

        label = dict(cli.input_manager.choose.call_args.args[1])["a1"]
        self.assertIn("google-pinger", label)
        self.assertIn("recon", label)
        self.assertIn("ping 10.0.0.5", label)
        self.assertIn("exec", label)
        self.assertIn("iter 6/30", label)

    def test_focused_row_uses_active_choice_label(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")
        cli.input_manager.choose = _create_mock_chooser(None)

        _run(AgentsMessage().execute(cli))

        options = dict(cli.input_manager.choose.call_args.args[1])
        style, text = list(options["a1"])[0]
        self.assertEqual(style, "class:active-choice")
        self.assertIn("google-pinger", text)
        self.assertIsInstance(options[MASTER_FOCUS], str)

    def test_master_row_is_active_when_not_attached(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.input_manager.choose = _create_mock_chooser(None)

        _run(AgentsMessage().execute(cli))

        options = dict(cli.input_manager.choose.call_args.args[1])
        self.assertEqual(list(options[MASTER_FOCUS])[0][0], "class:active-choice")
        self.assertIsInstance(options["a1"], str)

    def test_selecting_an_agent_attaches(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.input_manager.choose = _create_mock_chooser("a1")

        _run(AgentsMessage().execute(cli))

        cli.agent_attach.attach.assert_called_once_with(session)

    def test_selecting_master_detaches(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")
        cli.input_manager.choose = _create_mock_chooser(MASTER_FOCUS)

        _run(AgentsMessage().execute(cli))

        cli.agent_attach.detach.assert_called_once_with()

    def test_cancelling_the_picker_changes_nothing(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.input_manager.choose = _create_mock_chooser(None)

        _run(AgentsMessage().execute(cli))

        cli.agent_attach.attach.assert_not_called()
        cli.agent_attach.detach.assert_not_called()
        self.assertTrue(cli.agent_focus.is_master)

    def test_target_resolves_by_name_without_opening_the_picker(self):
        session = _create_mock_session("a1", "google-pinger")
        cli = _create_mock_cli(session)
        cli.input_manager.choose = MagicMock()

        _run(AgentsMessage("google-pinger").execute(cli))

        cli.input_manager.choose.assert_not_called()
        cli.agent_attach.attach.assert_called_once_with(session)

    def test_target_resolves_by_id(self):
        session = _create_mock_session("a1", "google-pinger")
        cli = _create_mock_cli(session)

        _run(AgentsMessage("a1").execute(cli))

        cli.agent_attach.attach.assert_called_once_with(session)

    def test_unknown_target_is_reported_not_attached(self):
        cli = _create_mock_cli(_create_mock_session("a1"))

        _run(AgentsMessage("nope").execute(cli))

        cli.agent_attach.attach.assert_not_called()
        self.assertTrue(cli.agent_focus.is_master)

    def test_reattaching_to_the_focused_agent_is_a_no_op(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        _run(AgentsMessage("a1").execute(cli))

        cli.agent_attach.attach.assert_not_called()

    def test_back_detaches(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        _run(BackMessage().execute(cli))

        cli.agent_attach.detach.assert_called_once_with()

    def test_back_on_master_does_not_detach(self):
        cli = _create_mock_cli(_create_mock_session("a1"))

        _run(BackMessage().execute(cli))

        cli.agent_attach.detach.assert_not_called()


@patch("kerkov.cli.kerkov_cli.console", MagicMock())
class AttachedInputRoutingTest(unittest.TestCase):

    def test_chat_is_held_back_while_attached(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        self.assertTrue(cli._reject_input_while_attached(GeneralMessage("whoami")))

    def test_chat_reaches_the_master_when_detached(self):
        cli = _create_mock_cli(_create_mock_session("a1"))

        self.assertFalse(cli._reject_input_while_attached(GeneralMessage("whoami")))

    def test_empty_input_is_not_treated_as_agent_chat(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        self.assertFalse(cli._reject_input_while_attached(GeneralMessage("   ")))


class PromptPrefixTest(unittest.TestCase):

    def test_master_prefix_uses_the_mode(self):
        cli = _create_mock_cli(_create_mock_session("a1"))

        self.assertIn("You:", cli._prompt_html())

    def test_attached_prefix_names_the_agent_and_profile(self):
        cli = _create_mock_cli(_create_mock_session("a1", "google-pinger", "recon"))
        cli.agent_focus.focus_agent("a1")

        prefix = cli._prompt_html()

        self.assertIn("google-pinger (recon)", prefix)
        self.assertNotIn("You:", prefix)

    def test_unresolvable_focus_falls_back_to_master(self):
        cli = _create_mock_cli()
        cli.agent_focus.focus_agent("a9")

        prefix = cli._prompt_html()

        self.assertIn("You:", prefix)
        self.assertTrue(cli.agent_focus.is_master)


if __name__ == "__main__":
    unittest.main()
