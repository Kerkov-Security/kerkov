import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.resume_message import ResumeMessage
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.models.basic_models import Message


class ResumeMessageTest(unittest.TestCase):
    def test_resume_delegates_to_restore_engagement(self):
        engagement = SimpleNamespace(
            metadata=SimpleNamespace(
                id="eng123",
                mode="audit",
                verbosity="standard",
                approval_mode="normal",
            ),
            messages=[Message(role="system", content="system")],
        )
        cli = SimpleNamespace(
            engagement_store=MagicMock(),
            restore_engagement=MagicMock(return_value=engagement),
            settle_agents_for_conversation_switch=AsyncMock(),
        )

        with (
            patch("kerkov.cli.messages.resume_message.console"),
            patch(
                "kerkov.cli.messages.resume_message.get_current_workspace",
                return_value="/tmp/workspaces/eng123",
            ),
        ):
            asyncio.run(ResumeMessage("eng123").execute(cli))

        cli.restore_engagement.assert_called_once_with(
            "eng123", clear_session_rules=True
        )
        cli.settle_agents_for_conversation_switch.assert_awaited_once()

    def test_resume_reports_missing_engagement(self):
        cli = SimpleNamespace(
            engagement_store=MagicMock(),
            restore_engagement=MagicMock(return_value=None),
            settle_agents_for_conversation_switch=AsyncMock(),
        )

        with (
            patch("kerkov.cli.messages.resume_message.console"),
            patch(
                "kerkov.cli.messages.resume_message.get_current_workspace"
            ) as mock_workspace,
        ):
            asyncio.run(ResumeMessage("missing").execute(cli))

        cli.restore_engagement.assert_called_once_with(
            "missing", clear_session_rules=True
        )
        mock_workspace.assert_not_called()
        # A mistyped id must not cost the user their running agents.
        cli.settle_agents_for_conversation_switch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class ResumeAgentRunTest(unittest.TestCase):
    """An agent sub-run opens read-only: its conversation cannot be continued."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EngagementStore(store_dir=Path(self._tmp.name))
        self.parent = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.agent_id = self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=self.parent,
            parent_call_id="call-1",
            agent_type="recon",
            task_instruction="scan 10.0.0.5",
        )
        self.store.append_message(Message(role="system", content="you are recon"))
        self.store.append_message(Message(role="assistant", content="found port 22"))
        self.cli = SimpleNamespace(
            engagement_store=self.store,
            restore_engagement=MagicMock(),
            settle_agents_for_conversation_switch=AsyncMock(),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _resume(self, engagement_id):
        with patch("kerkov.cli.messages.resume_message.console") as console:
            asyncio.run(ResumeMessage(engagement_id).execute(self.cli))
        return console

    def test_an_agent_id_is_never_restored(self):
        self._resume(self.agent_id)

        self.cli.restore_engagement.assert_not_called()
        self.cli.settle_agents_for_conversation_switch.assert_not_awaited()

    def test_the_transcript_and_its_parent_are_shown(self):
        with patch("kerkov.cli.agent_transcript.render_agent_transcript") as render:
            with patch("kerkov.cli.messages.resume_message.console"):
                asyncio.run(ResumeMessage(self.agent_id).execute(self.cli))

        metadata, messages = render.call_args.args[1], render.call_args.args[2]
        self.assertEqual(metadata.agent_type, "recon")
        self.assertEqual(metadata.parent_engagement_id, self.parent)
        self.assertEqual([m.content for m in messages][-1], "found port 22")

    def test_viewing_an_agent_does_not_change_the_active_append_target(self):
        self.store.load_engagement(self.parent)

        self._resume(self.agent_id)

        self.assertEqual(self.store.engagement_id, self.parent)

    def test_a_conversation_id_still_resumes(self):
        self._resume(self.parent)

        self.cli.restore_engagement.assert_called_once_with(
            self.parent, clear_session_rules=True
        )

    def test_the_picker_never_offers_an_agent_run(self):
        cli = SimpleNamespace(
            engagement_store=self.store,
            restore_engagement=MagicMock(return_value=None),
            settle_agents_for_conversation_switch=AsyncMock(),
            input_manager=MagicMock(get_line=AsyncMock(return_value="")),
        )

        with patch("kerkov.cli.messages.resume_message.console") as console:
            asyncio.run(ResumeMessage().execute(cli))

        table = console.print.call_args_list[0].args[0]
        listed = [cell for column in table.columns for cell in column.cells]
        self.assertIn(self.parent, listed)
        self.assertNotIn(self.agent_id, listed)
