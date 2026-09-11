"""Tests for `/engagements` and its opt-in agent sub-run listing."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kerkov.cli.engagement_store import EngagementStore
from kerkov.cli.input_parser import InputParser
from kerkov.cli.messages.engagements_message import EngagementsMessage


def _run(coro):
    return asyncio.run(coro)


def _rendered(mock_console) -> str:
    """Everything printed, flattened — tables included."""
    parts = []
    for call in mock_console.print.call_args_list:
        if not call.args:
            continue
        value = call.args[0]
        if hasattr(value, "columns"):
            parts.append(str(value.title))
            for column in value.columns:
                parts.extend(str(cell) for cell in column.cells)
        else:
            parts.append(str(value))
    return "\n".join(parts)


class EngagementsMessageTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EngagementStore(store_dir=Path(self._tmp.name))
        self.cli = SimpleNamespace(engagement_store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _start_conversation(self) -> str:
        return self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )

    def _start_agent_run(self, parent: str, task: str = "scan 10.0.0.5") -> str:
        return self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=parent,
            parent_call_id="call-1",
            agent_type="recon",
            task_instruction=task,
        )

    def test_conversations_view_hides_agent_runs(self):
        conversation = self._start_conversation()
        agent = self._start_agent_run(conversation)

        with patch(
            "kerkov.cli.messages.engagements_message.console", MagicMock()
        ) as console:
            _run(EngagementsMessage().execute(self.cli))

        output = _rendered(console)
        self.assertIn(conversation, output)
        self.assertNotIn(agent, output)
        self.assertIn("1 agent sub-run(s) hidden", output)

    def test_agents_view_lists_sub_runs_with_parent_linkage(self):
        conversation = self._start_conversation()
        agent = self._start_agent_run(conversation)

        with patch(
            "kerkov.cli.messages.engagements_message.console", MagicMock()
        ) as console:
            _run(EngagementsMessage(show_agents=True).execute(self.cli))

        output = _rendered(console)
        self.assertIn(agent, output)
        self.assertIn(conversation, output)
        self.assertIn("recon", output)
        self.assertIn("scan 10.0.0.5", output)

    def test_agents_view_says_so_when_there_are_none(self):
        self._start_conversation()

        with patch(
            "kerkov.cli.messages.engagements_message.console", MagicMock()
        ) as console:
            _run(EngagementsMessage(show_agents=True).execute(self.cli))

        self.assertIn("No agent sub-runs", _rendered(console))

    def test_agent_runs_do_not_crowd_out_conversations(self):
        """The listing filters before the limit, not after."""
        conversation = self._start_conversation()
        for _ in range(15):
            self._start_agent_run(conversation)

        with patch(
            "kerkov.cli.messages.engagements_message.console", MagicMock()
        ) as console:
            _run(EngagementsMessage().execute(self.cli))

        self.assertIn(conversation, _rendered(console))


class EngagementsParsingTest(unittest.TestCase):

    def test_bare_engagements_lists_conversations(self):
        message = InputParser.parse_for_message_type("/engagements")

        self.assertIsInstance(message, EngagementsMessage)
        self.assertFalse(message.show_agents)

    def test_agents_argument_opts_into_sub_runs(self):
        message = InputParser.parse_for_message_type("/engagements agents")

        self.assertTrue(message.show_agents)

    def test_an_unknown_argument_is_not_the_agents_view(self):
        message = InputParser.parse_for_message_type("/engagements nonsense")

        self.assertFalse(message.show_agents)


if __name__ == "__main__":
    unittest.main()
