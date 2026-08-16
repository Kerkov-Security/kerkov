"""Tests for KerkovCLI.restore_engagement."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kerkov.cli.approval_mode_manager import ApprovalMode
from kerkov.cli.engagement_store import EngagementMetadata, Engangement
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.modes import ModeType
from kerkov.cli.verbosity_manager import Verbosity
from kerkov.models.basic_models import Message


def _create_engagement(messages):
    metadata = EngagementMetadata(
        id="abc123",
        created_at="2026-06-24T00:00:00+00:00",
        updated_at="2026-06-24T00:00:00+00:00",
        mode="audit",
        model="claude-sonnet-4-5-20250929",
        verbosity="standard",
        approval_mode="normal",
    )
    return Engangement(metadata=metadata, messages=messages)


class RestoreEngagementTest(unittest.TestCase):
    """Tests for the shared resume state-restoration helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _create_cli(self, kb_enabled=True):
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.engagement_store = MagicMock()
        cli.engagement_store.store_dir = self.store_dir
        # A restored conversation is a root: no parent to walk up to.
        cli.engagement_store.parent_engagement_id_of.return_value = None
        cli.mode_manager = MagicMock()
        cli.verbosity_manager = MagicMock()
        cli.approval_mode_manager = MagicMock()
        cli.workspace_manager = MagicMock()
        cli.tool_approval_helper = MagicMock()
        cli.conversation_history = []
        cli.config = MagicMock(
            kb_enabled=kb_enabled, kb_embeddings=False, kb_embedding_provider=None
        )
        cli._knowledge_base = None
        return cli

    def test_returns_none_when_engagement_missing(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = None

        result = cli.restore_engagement("missing")

        self.assertIsNone(result)
        cli.mode_manager.switch_mode.assert_not_called()
        cli.workspace_manager.activate.assert_not_called()

    def test_restores_state_and_returns_engagement(self):
        cli = self._create_cli()
        messages = [Message(role="user", content="hello")]
        engagement = _create_engagement(messages)
        cli.engagement_store.load_engagement.return_value = engagement
        cli.workspace_manager.activate.return_value = "/tmp/workspace"

        with patch(
            "kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"
        ) as mock_apply:
            result = cli.restore_engagement("abc123")

        self.assertIs(result, engagement)
        cli.mode_manager.switch_mode.assert_called_once_with(mode_type=ModeType.AUDIT)
        cli.verbosity_manager.set.assert_called_once_with(Verbosity.STANDARD)
        cli.approval_mode_manager.set.assert_called_once_with(ApprovalMode.NORMAL)
        self.assertIs(cli.conversation_history, engagement.messages)
        mock_apply.assert_called_once_with(engagement.messages, Verbosity.STANDARD)
        cli.workspace_manager.activate.assert_called_once_with("abc123")
        cli.tool_approval_helper.set_workspace.assert_called_once_with("/tmp/workspace")

    def test_restores_engagement_with_zero_messages(self):
        cli = self._create_cli()
        engagement = _create_engagement([])
        cli.engagement_store.load_engagement.return_value = engagement

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            result = cli.restore_engagement("abc123")

        self.assertIs(result, engagement)
        self.assertEqual(cli.conversation_history, [])

    def test_clear_session_rules_flag_controls_clearing(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = _create_engagement([])

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123")
        cli.tool_approval_helper.clear_session_rules.assert_not_called()

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123", clear_session_rules=True)
        cli.tool_approval_helper.clear_session_rules.assert_called_once()

    def test_resume_reattaches_to_the_engagements_knowledge_base(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = _create_engagement([])

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123")

        self.assertIsNotNone(cli._knowledge_base)
        self.assertEqual(cli._knowledge_base.root_engagement_id, "abc123")

    def test_resuming_an_agent_run_attaches_to_its_masters_knowledge_base(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = _create_engagement([])
        cli.engagement_store.parent_engagement_id_of.side_effect = lambda eid: (
            "master-1" if eid == "abc123" else None
        )

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123")

        # Not a second store keyed by the sub-run's own id.
        self.assertEqual(cli._knowledge_base.root_engagement_id, "master-1")

    def test_no_knowledge_base_is_opened_when_disabled(self):
        cli = self._create_cli(kb_enabled=False)
        cli.engagement_store.load_engagement.return_value = _create_engagement([])

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123")

        self.assertIsNone(cli._knowledge_base)

    def test_a_broken_knowledge_base_does_not_break_resume(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = _create_engagement([])
        cli.engagement_store.parent_engagement_id_of.side_effect = OSError("bad disk")

        with patch("kerkov.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            result = cli.restore_engagement("abc123")

        self.assertIsNotNone(result)
        self.assertIsNone(cli._knowledge_base)


if __name__ == "__main__":
    unittest.main()
