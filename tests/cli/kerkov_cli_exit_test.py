"""Tests for CLI exit messaging."""

import unittest
from unittest.mock import MagicMock, patch

from src.cli.kerkov_cli import KerkovCLI


class KerkovCliExitTest(unittest.TestCase):
    """Tests for resume command output on exit."""

    def _create_cli_with_engagement(self, engagement_id):
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.engagement_store = MagicMock()
        cli.engagement_store.engagement_id = engagement_id
        return cli

    def test_resume_command_includes_active_engagement_id(self):
        cli = self._create_cli_with_engagement("abc123")

        self.assertEqual(
            cli.get_resume_command_message(),
            "Resume this session with: uv run kerkov --resume abc123",
        )

    def test_resume_command_falls_back_without_engagement_id(self):
        cli = self._create_cli_with_engagement(None)

        self.assertEqual(
            cli.get_resume_command_message(),
            "No active engagement has been saved yet, so there is no resume command.",
        )

    @patch("src.cli.kerkov_cli.console")
    def test_print_resume_command_uses_shared_message(self, mock_console):
        cli = self._create_cli_with_engagement("abc123")

        cli.print_resume_command()

        mock_console.print.assert_called_once_with(
            "[dim]Resume this session with: uv run kerkov --resume abc123[/dim]"
        )

    @patch("src.cli.kerkov_cli.console")
    def test_print_resume_command_is_idempotent(self, mock_console):
        cli = self._create_cli_with_engagement("abc123")

        cli.print_resume_command()
        cli.print_resume_command()

        mock_console.print.assert_called_once_with(
            "[dim]Resume this session with: uv run kerkov --resume abc123[/dim]"
        )


if __name__ == "__main__":
    unittest.main()
