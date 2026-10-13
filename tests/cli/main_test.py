"""Tests for CLI non-interactive entrypoints."""

import argparse
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli import main
from kerkov.cli.engagement_restore import EngagementRestoreError
from kerkov.cli.engagement_store import EngagementStore


class MainTest(unittest.TestCase):
    """Tests for kerkov.cli.main helpers and dispatch."""

    def test_parse_args_collects_repeated_messages(self):
        args = main.parse_args(
            ["--message", "/provider codex", "--message", "scan target"]
        )

        self.assertEqual(args.message, ["/provider codex", "scan target"])
        self.assertFalse(args.no_welcome)

    def test_parse_args_accepts_explicit_opengrep_setup(self):
        args = main.parse_args(["setup", "opengrep", "--timeout", "45"])

        self.assertEqual(args.command, "setup")
        self.assertEqual(args.component, "opengrep")
        self.assertEqual(args.timeout, 45)

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

    @patch("kerkov.cli.kerkov_cli.KerkovCLI")
    @patch("kerkov.cli.tool_setup.run_tool_setup", new_callable=AsyncMock)
    def test_main_runs_setup_without_initializing_chat(
        self, mock_run_tool_setup, mock_cli_cls
    ):
        mock_run_tool_setup.return_value = 0

        with patch("kerkov.cli.main.parse_args") as mock_parse_args:
            mock_parse_args.return_value = argparse.Namespace(
                command="setup",
                component="opengrep",
                timeout=45.0,
            )
            main.main()

        mock_run_tool_setup.assert_awaited_once_with(
            "opengrep",
            main.console,
            timeout_seconds=45.0,
        )
        mock_cli_cls.assert_not_called()

    @patch("kerkov.cli.main.asyncio.run")
    @patch("kerkov.cli.main._run_cli", new_callable=MagicMock)
    @patch("kerkov.cli.kerkov_cli.KerkovCLI")
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
                interface=None,
            )
            main.main()

        mock_run_cli.assert_called_once_with(
            cli,
            None,
            non_interactive_inputs=["/provider codex", "scan target"],
            show_welcome=False,
        )
        mock_asyncio_run.assert_called_once_with("run-cli-coro")


class ResumeEntryPointTest(unittest.TestCase):
    """`--resume`/`--continue` share the guards the `/resume` handler has."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EngagementStore(store_dir=Path(self._tmp.name))
        self.conversation = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.agent_id = self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=self.conversation,
            agent_type="recon",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _create_cli(self):
        cli = MagicMock()
        cli._initialize_provider = AsyncMock()
        cli.run = AsyncMock()
        cli.engagement_store = self.store
        return cli

    def test_an_agent_id_prints_its_transcript_instead_of_restoring(self):
        cli = self._create_cli()

        with patch("kerkov.cli.agent_transcript.render_agent_transcript") as render:
            asyncio.run(main._run_cli(cli, self.agent_id))

        render.assert_called_once()
        cli.restore_engagement.assert_not_called()
        cli.run.assert_not_awaited()

    def test_a_conversation_is_still_restored(self):
        cli = self._create_cli()

        asyncio.run(main._run_cli(cli, self.conversation))

        cli.restore_engagement.assert_called_once_with(self.conversation)
        cli.run.assert_awaited_once_with()

    def test_an_unrestorable_engagement_exits_with_a_message(self):
        cli = self._create_cli()
        cli.restore_engagement.side_effect = EngagementRestoreError("bad verbosity")

        with patch("kerkov.cli.main.console") as console:
            with self.assertRaises(SystemExit) as exit_info:
                asyncio.run(main._run_cli(cli, self.conversation))

        self.assertEqual(exit_info.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        self.assertIn("bad verbosity", printed)


if __name__ == "__main__":
    unittest.main()
