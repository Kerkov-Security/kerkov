"""Tests for agent focus: routing, prompt prefix, and the `/agents` picker."""

import asyncio
import contextlib
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
    cli.output_sink = MagicMock()
    cli.output_sink.suspend_status.return_value = contextlib.nullcontext()
    cli.console = MagicMock()
    cli._switcher_task = None
    cli._switcher_handoff = None
    return cli


def _create_mock_input_manager_for_switcher() -> MagicMock:
    """A real `switcher_requested` event on an otherwise mocked manager."""
    manager = MagicMock()
    manager.switcher_requested = asyncio.Event()
    manager.is_watching_for_interrupt = True
    manager.wait_for_terminal = AsyncMock()
    manager.picker_ownership.return_value = contextlib.nullcontext()
    return manager


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

    def test_agents_with_target_defaults_to_attaching(self):
        message = InputParser.parse_for_message_type("/agents a1")

        self.assertEqual(message.action, "attach")

    def test_agents_stop_parses_to_a_stop_action(self):
        message = InputParser.parse_for_message_type("/agents stop a1")

        self.assertIsInstance(message, AgentsMessage)
        self.assertEqual(message.action, "stop")
        self.assertEqual(message.target, "a1")

    def test_bare_agents_stop_is_an_error_not_stop_everything(self):
        """Mass cancel already has a name (`/q`); an accidental one is final."""
        message = InputParser.parse_for_message_type("/agents stop")

        self.assertEqual(message.action, "invalid")

    def test_back_is_registered(self):
        self.assertIsInstance(InputParser.parse_for_message_type("/back"), BackMessage)


@patch("kerkov.cli.messages.agents_message.console", MagicMock())
class AgentsStopTest(unittest.TestCase):

    def test_stopping_the_focused_agent_detaches_first(self):
        """The view would otherwise keep streaming an agent the user just stopped."""
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.agent_focus.focus_agent("a1")
        cli.agent_supervisor.cancel = _create_mock_chooser(True)

        _run(AgentsMessage("a1", action="stop").execute(cli))

        cli.agent_attach.detach.assert_called_once()
        cli.agent_supervisor.cancel.assert_called_once_with("a1")

    def test_stopping_an_unattached_agent_leaves_focus_alone(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.agent_supervisor.cancel = _create_mock_chooser(True)

        _run(AgentsMessage("a1", action="stop").execute(cli))

        cli.agent_attach.detach.assert_not_called()
        cli.agent_supervisor.cancel.assert_called_once_with("a1")

    def test_stopping_an_unknown_agent_cancels_nothing(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_supervisor.cancel = _create_mock_chooser(True)

        _run(AgentsMessage("nope", action="stop").execute(cli))

        cli.agent_supervisor.cancel.assert_not_called()

    def test_stopping_a_finished_agent_cancels_nothing(self):
        session = _create_mock_session("a1", state=AgentState.DONE)
        cli = _create_mock_cli(session)
        cli.agent_supervisor.cancel = _create_mock_chooser(True)

        _run(AgentsMessage("a1", action="stop").execute(cli))

        cli.agent_supervisor.cancel.assert_not_called()


@patch("kerkov.cli.messages.agents_message.console", MagicMock())
class AgentsReviewTest(unittest.TestCase):
    """`/agents review` — the opt-in that puts an agent's calls to the operator."""

    def test_review_is_off_until_asked_for(self):
        session = _create_mock_session("a1")

        self.assertFalse(session.review_tools)

    def test_review_toggles_when_no_value_is_given(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)

        _run(AgentsMessage("a1", action="review").execute(cli))
        self.assertTrue(session.review_tools)

        _run(AgentsMessage("a1", action="review").execute(cli))
        self.assertFalse(session.review_tools)

    def test_review_off_is_idempotent(self):
        session = _create_mock_session("a1")
        session.review_tools = True
        cli = _create_mock_cli(session)

        _run(AgentsMessage("a1", action="review", value=False).execute(cli))
        _run(AgentsMessage("a1", action="review", value=False).execute(cli))

        self.assertFalse(session.review_tools)

    def test_review_resolves_by_name(self):
        session = _create_mock_session("a1", "google-pinger")
        cli = _create_mock_cli(session)

        _run(AgentsMessage("google-pinger", action="review", value=True).execute(cli))

        self.assertTrue(session.review_tools)

    def test_review_on_an_unknown_agent_changes_nothing(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)

        _run(AgentsMessage("nope", action="review", value=True).execute(cli))

        self.assertFalse(session.review_tools)

    def test_review_on_a_finished_agent_changes_nothing(self):
        session = _create_mock_session("a1", state=AgentState.DONE)
        cli = _create_mock_cli(session)

        _run(AgentsMessage("a1", action="review", value=True).execute(cli))

        self.assertFalse(session.review_tools)


class AgentsReviewParsingTest(unittest.TestCase):

    def test_review_with_a_target_toggles(self):
        message = InputParser.parse_for_message_type("/agents review a1")

        self.assertIsInstance(message, AgentsMessage)
        self.assertEqual(message.action, "review")
        self.assertEqual(message.target, "a1")
        self.assertIsNone(message.value)

    def test_review_on_and_off_are_explicit(self):
        on = InputParser.parse_for_message_type("/agents review a1 on")
        off = InputParser.parse_for_message_type("/agents review a1 off")

        self.assertTrue(on.value)
        self.assertFalse(off.value)

    def test_bare_review_is_an_error_not_a_blanket_switch(self):
        message = InputParser.parse_for_message_type("/agents review")

        self.assertEqual(message.action, "invalid")

    def test_an_unknown_third_word_is_an_error(self):
        message = InputParser.parse_for_message_type("/agents review a1 maybe")

        self.assertEqual(message.action, "invalid")


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

    def test_chat_is_queued_on_the_focused_agents_inbox(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.agent_focus.focus_agent("a1")

        handled = cli._route_input_to_focused_agent(GeneralMessage("whoami"))

        self.assertTrue(handled)
        self.assertEqual(session.inbox.get_nowait(), "whoami")

    def test_the_queued_line_is_echoed_into_the_agents_own_buffer(self):
        """So it shows up inline while attached, and again on a re-attach."""
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.agent_focus.focus_agent("a1")

        cli._route_input_to_focused_agent(GeneralMessage("whoami"))

        self.assertIn("> whoami", session.events.buffer)

    def test_input_reaches_neither_a_sibling_nor_the_master(self):
        focused = _create_mock_session("a1")
        sibling = _create_mock_session("a2", name="other")
        cli = _create_mock_cli(focused, sibling)
        cli.agent_focus.focus_agent("a1")

        cli._route_input_to_focused_agent(GeneralMessage("whoami"))

        self.assertEqual(sibling.inbox.qsize(), 0)
        self.assertEqual(focused.inbox.qsize(), 1)

    def test_chat_reaches_the_master_when_detached(self):
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)

        self.assertFalse(cli._route_input_to_focused_agent(GeneralMessage("whoami")))
        self.assertEqual(session.inbox.qsize(), 0)

    def test_empty_input_is_not_treated_as_agent_chat(self):
        cli = _create_mock_cli(_create_mock_session("a1"))
        cli.agent_focus.focus_agent("a1")

        self.assertFalse(cli._route_input_to_focused_agent(GeneralMessage("   ")))

    def test_input_to_a_finished_agent_is_refused_not_queued(self):
        session = _create_mock_session("a1", state=AgentState.DONE)
        cli = _create_mock_cli(session)
        cli.agent_focus.focus_agent("a1")

        handled = cli._route_input_to_focused_agent(GeneralMessage("whoami"))

        self.assertTrue(handled)
        self.assertEqual(session.inbox.qsize(), 0)

    def test_slash_commands_are_never_routed_to_the_agent(self):
        """The routing gate is the message type, so `/back` always works."""
        session = _create_mock_session("a1")
        cli = _create_mock_cli(session)
        cli.agent_focus.focus_agent("a1")

        parsed = InputParser.parse_for_message_type("/back")

        self.assertIsInstance(parsed, BackMessage)
        self.assertNotIsInstance(parsed, GeneralMessage)
        self.assertEqual(session.inbox.qsize(), 0)

    def test_a_stale_focus_falls_back_to_master_without_routing(self):
        cli = _create_mock_cli()
        cli.agent_focus.focus_agent("a9")

        handled = cli._route_input_to_focused_agent(GeneralMessage("whoami"))

        self.assertTrue(handled)
        self.assertTrue(cli.agent_focus.is_master)


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


class MidTurnSwitcherTaskTest(unittest.TestCase):
    """`Ctrl-O` during a turn: the picker opens without the turn stopping."""

    def _create_cli(self):
        cli = _create_mock_cli(_create_mock_session("a1", "google-pinger"))
        cli.input_manager = _create_mock_input_manager_for_switcher()
        cli.input_manager.choose = _create_mock_chooser(None)
        return cli

    def test_the_event_opens_the_picker_and_the_watcher_is_restored(self):
        cli = self._create_cli()
        opened = []

        async def _open(_message):
            opened.append(True)

        cli._execute_user_message_safely = _open

        async def _scenario():
            cli._start_switcher_watch()
            cli.input_manager.switcher_requested.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if opened:
                    break
            await cli._stop_switcher_watch()

        _run(_scenario())

        self.assertTrue(opened)
        cli.input_manager.picker_ownership.assert_called_once_with()

    def test_the_watcher_is_left_off_when_the_turn_was_between_awaits(self):
        cli = self._create_cli()
        cli.input_manager.is_watching_for_interrupt = False
        cli._execute_user_message_safely = MagicMock(
            side_effect=lambda _m: asyncio.sleep(0)
        )

        async def _scenario():
            cli._start_switcher_watch()
            cli.input_manager.switcher_requested.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if cli.input_manager.picker_ownership.called:
                    break
            await cli._stop_switcher_watch()

        _run(_scenario())

        cli.input_manager.picker_ownership.assert_called_once_with()

    def test_a_turn_that_ends_mid_picker_lets_the_choice_finish(self):
        cli = self._create_cli()
        release = None
        finished = []

        async def _open(_message):
            await release.wait()
            finished.append(True)

        cli._execute_user_message_safely = _open

        async def _scenario():
            nonlocal release
            release = asyncio.Event()
            cli._start_switcher_watch()
            cli.input_manager.switcher_requested.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if cli._switcher_handoff is not None:
                    break
            stop = asyncio.ensure_future(cli._stop_switcher_watch())
            await asyncio.sleep(0)
            self.assertFalse(finished)
            release.set()
            await stop

        _run(_scenario())

        self.assertTrue(finished)
        # Whatever the picker left behind, prompt_toolkit is next for stdin.
        cli.input_manager.stop_watching_for_interrupt.assert_called()

    def test_stopping_a_watch_that_never_started_is_a_no_op(self):
        cli = self._create_cli()

        _run(cli._stop_switcher_watch())

        cli.input_manager.stop_watching_for_interrupt.assert_not_called()

    def test_starting_twice_keeps_one_task(self):
        cli = self._create_cli()

        async def _scenario():
            cli._start_switcher_watch()
            first = cli._switcher_task
            cli._start_switcher_watch()
            self.assertIs(cli._switcher_task, first)
            await cli._stop_switcher_watch()

        _run(_scenario())


if __name__ == "__main__":
    unittest.main()
