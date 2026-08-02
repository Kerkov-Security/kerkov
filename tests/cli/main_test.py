"""Tests for CLI non-interactive entrypoints."""

import argparse
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli import main


class MainTest(unittest.TestCase):
    """Tests for kerkov.cli.main helpers and dispatch."""

    def test_parse_args_collects_repeated_messages(self):
        args = main.parse_args(
            ["--message", "/provider codex", "--message", "scan target"]
        )

        self.assertEqual(args.message, ["/provider codex", "scan target"])
        self.assertFalse(args.no_welcome)

    def test_collect_non_interactive_inputs_prefers_explicit_messages(self):
        args = argparse.Namespace(message=["first", "second"])

        with patch.object(main.sys.stdin, "isatty", return_value=False):
            self.assertEqual(
                main.collect_non_interactive_inputs(args), ["first", "second"]
            )

    def test_collect_non_interactive_inputs_reads_piped_stdin(self):
        args = argparse.Namespace(message=[])

        with patch.object(main.sys.stdin, "isatty", return_value=False):
            with patch.object(
                main.sys.stdin, "read", return_value="/provider codex\n\nscan target\n"
            ):
                self.assertEqual(
                    main.collect_non_interactive_inputs(args),
                    ["/provider codex", "scan target"],
                )

    def test_run_cli_initializes_provider_before_dispatch(self):
        cli = MagicMock()
        cli._initialize_provider = AsyncMock()
        cli.run = AsyncMock()

        asyncio.run(main._run_cli(cli))

        cli._initialize_provider.assert_awaited_once_with()
        cli.run.assert_awaited_once_with()

    @patch("kerkov.cli.main.asyncio.run")
    @patch("kerkov.cli.main._run_cli", new_callable=MagicMock)
    @patch("kerkov.cli.main.KerkovCLI")
    def test_main_runs_non_interactive_when_messages_present(
        self, mock_cli_cls, mock_run_cli, mock_asyncio_run
    ):
        cli = MagicMock()
        mock_cli_cls.return_value = cli
        mock_run_cli.return_value = "run-cli-coro"

        with patch("kerkov.cli.main.parse_args") as mock_parse_args:
            mock_parse_args.return_value = argparse.Namespace(
                mode="audit",
                continue_engagement=False,
                resume=None,
                message=["/provider codex", "scan target"],
                no_welcome=True,
                yolo=False,
                verbosity=None,
                approval=None,
            )
            main.main()

        mock_run_cli.assert_called_once_with(
            cli,
            None,
            non_interactive_inputs=["/provider codex", "scan target"],
            show_welcome=False,
        )
        mock_asyncio_run.assert_called_once_with("run-cli-coro")


if __name__ == "__main__":
    unittest.main()
