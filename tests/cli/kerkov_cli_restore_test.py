"""Tests for KerkovCLI.restore_engagement."""

import unittest
from unittest.mock import MagicMock, patch

from src.cli.approval_mode_manager import ApprovalMode
from src.cli.engagement_store import Engangement, EngagementMetadata
from src.cli.kerkov_cli import KerkovCLI
from src.cli.modes import ModeType
from src.cli.verbosity_manager import Verbosity
from src.models.basic_models import Message


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

    def _create_cli(self):
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.engagement_store = MagicMock()
        cli.mode_manager = MagicMock()
        cli.verbosity_manager = MagicMock()
        cli.approval_mode_manager = MagicMock()
        cli.workspace_manager = MagicMock()
        cli.tool_approval_helper = MagicMock()
        cli.conversation_history = []
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

        with patch("src.cli.kerkov_cli.apply_verbosity_to_system_prompt") as mock_apply:
            result = cli.restore_engagement("abc123")

        self.assertIs(result, engagement)
        cli.mode_manager.switch_mode.assert_called_once_with(mode_type=ModeType.AUDIT)
        cli.verbosity_manager.set.assert_called_once_with(Verbosity.STANDARD)
        cli.approval_mode_manager.set.assert_called_once_with(ApprovalMode.NORMAL)
        self.assertIs(cli.conversation_history, messages)
        mock_apply.assert_called_once_with(messages, Verbosity.STANDARD)
        cli.workspace_manager.activate.assert_called_once_with("abc123")
        cli.tool_approval_helper.set_workspace.assert_called_once_with("/tmp/workspace")

    def test_restores_engagement_with_zero_messages(self):
        cli = self._create_cli()
        engagement = _create_engagement([])
        cli.engagement_store.load_engagement.return_value = engagement

        with patch("src.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            result = cli.restore_engagement("abc123")

        self.assertIs(result, engagement)
        self.assertEqual(cli.conversation_history, [])

    def test_clear_session_rules_flag_controls_clearing(self):
        cli = self._create_cli()
        cli.engagement_store.load_engagement.return_value = _create_engagement([])

        with patch("src.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123")
        cli.tool_approval_helper.clear_session_rules.assert_not_called()

        with patch("src.cli.kerkov_cli.apply_verbosity_to_system_prompt"):
            cli.restore_engagement("abc123", clear_session_rules=True)
        cli.tool_approval_helper.clear_session_rules.assert_called_once()


if __name__ == "__main__":
    unittest.main()
