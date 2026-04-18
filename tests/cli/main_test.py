"""Tests for CLI non-interactive entrypoints."""

import argparse
import unittest
from unittest.mock import MagicMock, patch

from src.cli import main


class MainTest(unittest.TestCase):
    """Tests for src.cli.main helpers and dispatch."""

    def test_parse_args_collects_repeated_messages(self):
        args = main.parse_args(["--message", "/provider codex", "--message", "scan target"])

        self.assertEqual(args.message, ["/provider codex", "scan target"])
        self.assertFalse(args.no_welcome)

    def test_collect_non_interactive_inputs_prefers_explicit_messages(self):
        args = argparse.Namespace(message=["first", "second"])

        with patch.object(main.sys.stdin, "isatty", return_value=False):
            self.assertEqual(main.collect_non_interactive_inputs(args), ["first", "second"])

    def test_collect_non_interactive_inputs_reads_piped_stdin(self):
        args = argparse.Namespace(message=[])

        with patch.object(main.sys.stdin, "isatty", return_value=False):
            with patch.object(main.sys.stdin, "read", return_value="/provider codex\n\nscan target\n"):
                self.assertEqual(
                    main.collect_non_interactive_inputs(args),
                    ["/provider codex", "scan target"],
                )

    @patch("src.cli.main.asyncio.run")
    @patch("src.cli.main.KerkovCLI")
    def test_main_runs_non_interactive_when_messages_present(self, mock_cli_cls, mock_asyncio_run):
        cli = MagicMock()
        cli.run_non_interactive.return_value = "non-interactive-coro"
        mock_cli_cls.return_value = cli

        with patch("src.cli.main.parse_args") as mock_parse_args:
            mock_parse_args.return_value = argparse.Namespace(
                mode="audit",
                continue_engagement=False,
                resume=None,
                message=["/provider codex", "scan target"],
                no_welcome=True,
            )
            main.main()

        cli.run_non_interactive.assert_called_once_with(
            ["/provider codex", "scan target"],
            show_welcome=False,
        )
        mock_asyncio_run.assert_called_once_with("non-interactive-coro")


if __name__ == "__main__":
    unittest.main()
