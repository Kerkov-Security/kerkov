"""Tests for the attached view: replay on attach, then stream, then detach."""

import io
import time
import unittest
from unittest.mock import MagicMock

from rich.console import Console

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.cli.agent_attach import (
    REPLAY_TAIL_LINES,
    STREAM_LINE_CHARS,
    AgentAttachView,
)
from kerkov.cli.agent_focus_manager import AgentFocusManager
from kerkov.cli.buffering_sink import BufferingSink
from kerkov.core.conversation_engine import ConsoleSink


def _create_mock_supervisor(*sessions: AgentSession) -> AgentSupervisor:
    supervisor = AgentSupervisor(AgentRegistry(max_depth=2, max_total=20))
    for session in sessions:
        session.on_event = supervisor._fan_out
        supervisor._sessions[session.id] = session
    return supervisor


def _create_mock_session(agent_id: str = "a1", name: str = "google-pinger"):
    session = AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile="recon",
        task="ping 10.0.0.5",
        name=name,
    )
    session.state = AgentState.RUNNING
    session.started_at = time.monotonic()
    return session


def _printed(console: MagicMock) -> str:
    return "\n".join(
        str(call.args[0]) if call.args else "" for call in console.print.mock_calls
    )


class AgentAttachViewTest(unittest.TestCase):

    def setUp(self):
        self.console = MagicMock()
        self.session = _create_mock_session()
        self.supervisor = _create_mock_supervisor(self.session)
        self.focus = AgentFocusManager()
        self.view = AgentAttachView(self.console, self.supervisor, self.focus)

    def test_attach_sets_focus_and_replays_the_buffer(self):
        self.session.events.info("nmap started")
        self.session.events.info("port 22 open")

        self.view.attach(self.session)

        self.assertEqual(self.focus.agent_id, "a1")
        output = _printed(self.console)
        self.assertIn("nmap started", output)
        self.assertIn("port 22 open", output)
        self.assertIn("google-pinger (recon)", output)

    def test_replayed_lines_are_not_streamed_again(self):
        self.session.events.info("nmap started")
        self.view.attach(self.session)
        self.console.reset_mock()

        self.session.events.info("port 80 open")

        output = _printed(self.console)
        self.assertIn("port 80 open", output)
        self.assertNotIn("nmap started", output)

    def test_events_stream_while_attached(self):
        self.view.attach(self.session)
        self.console.reset_mock()

        self.session.events.tool_start("exec", {"cmd": "nmap"})
        self.session.events.tool_succeeded("exec", MagicMock(error=None))

        output = _printed(self.console)
        self.assertIn("exec", output)

    def test_state_changes_are_announced(self):
        self.view.attach(self.session)
        self.console.reset_mock()

        self.session.set_state(AgentState.REPORTING)

        self.assertIn("reporting", _printed(self.console))

    def test_other_agents_do_not_leak_into_the_attached_view(self):
        other = _create_mock_session("a2", "log-parser")
        other.on_event = self.supervisor._fan_out
        self.supervisor._sessions["a2"] = other
        self.view.attach(self.session)
        self.console.reset_mock()

        other.events.info("noise from a2")

        self.assertNotIn("noise from a2", _printed(self.console))

    def test_detach_returns_focus_and_stops_streaming(self):
        self.view.attach(self.session)

        detached = self.view.detach()

        self.assertEqual(detached, "a1")
        self.assertTrue(self.focus.is_master)
        self.console.reset_mock()
        self.session.events.info("after detach")
        self.assertNotIn("after detach", _printed(self.console))

    def test_detach_on_master_is_a_no_op(self):
        self.assertIsNone(self.view.detach())
        self.assertTrue(self.focus.is_master)

    def test_switching_agents_leaves_one_subscription(self):
        other = _create_mock_session("a2", "log-parser")
        other.on_event = self.supervisor._fan_out
        self.supervisor._sessions["a2"] = other

        self.view.attach(self.session)
        self.view.attach(other)

        self.assertEqual(len(self.supervisor._subscribers), 1)
        self.assertEqual(self.focus.agent_id, "a2")

    def test_switching_agents_replays_the_new_agent_from_its_own_start(self):
        other = _create_mock_session("a2", "log-parser")
        other.on_event = self.supervisor._fan_out
        self.supervisor._sessions["a2"] = other
        other.events.info("parsed 40 lines")

        self.view.attach(self.session)
        self.console.reset_mock()
        self.view.attach(other)

        self.assertIn("parsed 40 lines", _printed(self.console))

    def test_long_buffers_replay_only_the_tail(self):
        for n in range(REPLAY_TAIL_LINES + 50):
            self.session.events.info(f"line {n}")

        self.view.attach(self.session)

        output = _printed(self.console)
        self.assertIn("50 earlier line(s) omitted", output)
        self.assertNotIn("line 0\n", output)
        self.assertIn(f"line {REPLAY_TAIL_LINES + 49}", output)

    def test_attaching_to_a_finished_agent_replays_and_says_so(self):
        self.session.events.info("scan complete")
        self.session.state = AgentState.DONE
        self.session.finished_at = time.monotonic()

        self.view.attach(self.session)

        output = _printed(self.console)
        self.assertIn("scan complete", output)
        self.assertIn("has finished", output)


class AgentAttachRenderingTest(unittest.TestCase):
    """Renders through a real `Console`, which a mock cannot vouch for.

    Everything here is text the agent produced: it reaches the terminal only
    after Rich has parsed it, so asserting on the call argument proves nothing.
    """

    def setUp(self):
        self.buffer = io.StringIO()
        # no_color so assertions read the text, not the escape sequences.
        self.console = Console(file=self.buffer, width=200, no_color=True)
        self.session = _create_mock_session()
        self.supervisor = _create_mock_supervisor(self.session)
        self.view = AgentAttachView(self.console, self.supervisor, AgentFocusManager())

    def _rendered(self) -> str:
        return self.buffer.getvalue()

    def test_state_banner_survives_rich(self):
        self.view.attach(self.session)

        self.session.set_state(AgentState.REPORTING)

        self.assertIn("[reporting]", self._rendered())

    def test_agent_text_is_not_reinterpreted_as_markup(self):
        self.session.events.info("found [bold]/etc/passwd[/bold] readable")

        self.view.attach(self.session)

        self.assertIn("[bold]/etc/passwd[/bold] readable", self._rendered())

    def test_long_lines_are_trimmed_for_display_only(self):
        line = "x" * (STREAM_LINE_CHARS + 500)
        self.session.events.info(line)

        self.view.attach(self.session)

        self.assertIn("…", self._rendered())
        self.assertNotIn("x" * (STREAM_LINE_CHARS + 1), self._rendered())
        # The buffer keeps the whole line; only the view trims.
        self.assertIn(line, self.session.events.buffer)

    def test_multiline_records_keep_the_stream_prefix(self):
        self.session.events.info("line one\nline two")

        self.view.attach(self.session)

        rendered = self._rendered()
        self.assertIn("│ line one", rendered)
        self.assertIn("│ line two", rendered)


class AgentAttachBufferingTest(unittest.TestCase):
    """Master narration is held back while the terminal is showing an agent."""

    def setUp(self):
        self.buffer = io.StringIO()
        self.console = Console(file=self.buffer, width=200, no_color=True)
        self.sink = BufferingSink(ConsoleSink(self.console))
        self.session = _create_mock_session()
        self.supervisor = _create_mock_supervisor(self.session)
        self.view = AgentAttachView(
            self.console, self.supervisor, AgentFocusManager(), self.sink
        )

    def test_attaching_starts_buffering(self):
        self.view.attach(self.session)

        self.assertTrue(self.sink.buffering)

    def test_master_output_produced_while_attached_is_withheld(self):
        self.view.attach(self.session)
        marker = len(self.buffer.getvalue())

        self.sink.info("master keeps working")
        self.session.events.info("agent keeps working")

        since_attach = self.buffer.getvalue()[marker:]
        self.assertIn("agent keeps working", since_attach)
        self.assertNotIn("master keeps working", since_attach)

    def test_detaching_flushes_the_master_output_in_order(self):
        self.view.attach(self.session)
        self.sink.info("master line one")
        self.sink.info("master line two")

        self.view.detach()

        rendered = self.buffer.getvalue()
        self.assertFalse(self.sink.buffering)
        self.assertLess(
            rendered.index("master line one"), rendered.index("master line two")
        )
        self.assertLess(
            rendered.index("master line two"),
            rendered.index("Back on the main conversation"),
        )
        self.assertIn("main conversation while you were away", rendered)

    def test_a_quiet_master_gets_no_flush_header(self):
        self.view.attach(self.session)

        self.view.detach()

        self.assertNotIn("while you were away", self.buffer.getvalue())

    def test_switching_agents_keeps_buffering(self):
        other = _create_mock_session("a2", "port-scanner")
        self.supervisor._sessions["a2"] = other
        other.on_event = self.supervisor._fan_out
        self.view.attach(self.session)
        self.sink.info("master line during a1")

        self.view.attach(other)

        self.assertTrue(self.sink.buffering)
        self.assertNotIn("master line during a1", self.buffer.getvalue())

    def test_detaching_without_a_sink_still_works(self):
        view = AgentAttachView(self.console, self.supervisor, AgentFocusManager())

        view.attach(self.session)
        view.detach()

        self.assertIn("Back on the main conversation", self.buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
