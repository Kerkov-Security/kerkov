"""Tests for system prompt assembly on the first message."""

import unittest
from unittest.mock import MagicMock, patch

from src.cli.kerkov_cli import KerkovCLI
from src.cli.verbosity_prompts import VERBOSITY_MARKER, VERBOSITY_PROMPTS
from src.cli.verbosity_manager import Verbosity


class ApplySystemPromptTest(unittest.TestCase):
    """Regression tests for KerkovCLI._apply_system_prompt_if_possible."""

    def _create_cli(self):
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.conversation_history = []

        cli.mode_manager = MagicMock()
        cli.mode_manager.current_mode.system_prompt = "BASE PROMPT"
        cli.mode_manager.current_mode.name = "AUDIT"

        cli.skill_manager = MagicMock()
        cli.skill_manager.get_index_summary.return_value = ""

        cli.agent_manager = MagicMock()
        cli.agent_manager.get_index_summary.return_value = ""

        cli.verbosity_manager = MagicMock()
        cli.verbosity_manager.current = Verbosity.STANDARD

        cli.approval_mode_manager = MagicMock()
        cli.approval_mode_manager.current.value = "standard"

        cli.provder_manager = MagicMock()
        cli.provder_manager.provider.model = "claude-test"

        cli.engagement_store = MagicMock()
        cli.engagement_store.start_engagement.return_value = "eng123"

        cli.workspace_manager = MagicMock()
        cli.workspace_manager.activate.return_value = "/tmp/eng123"

        cli.tool_approval_helper = MagicMock()
        return cli

    @patch("src.cli.kerkov_cli.console")
    def test_system_prompt_is_str_with_verbosity_appended(self, _mock_console):
        cli = self._create_cli()

        cli._apply_system_prompt_if_possible()

        self.assertEqual(len(cli.conversation_history), 1)
        message = cli.conversation_history[0]
        self.assertEqual(message.role, "system")
        # Regression: a trailing comma previously made this a tuple, crashing
        # the str += with "can only concatenate str (not 'tuple') to str".
        self.assertIsInstance(message.content, str)
        self.assertEqual(
            message.content,
            "BASE PROMPT" + VERBOSITY_MARKER + VERBOSITY_PROMPTS[Verbosity.STANDARD],
        )

    @patch("src.cli.kerkov_cli.console")
    def test_noop_when_history_not_empty(self, _mock_console):
        cli = self._create_cli()
        cli.conversation_history = [MagicMock()]

        cli._apply_system_prompt_if_possible()

        cli.engagement_store.start_engagement.assert_not_called()


if __name__ == "__main__":
    unittest.main()
