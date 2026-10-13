"""Tests for SwitchAutonomyMessage (preset facade)."""

import asyncio
import unittest
from unittest.mock import MagicMock

from kerkov.cli.messages.switch_autonomy_message import SwitchAutonomyMessage
from kerkov.core.session.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.core.session.autonomy_manager import AutonomyLevel, AutonomyManager
from kerkov.core.session.verbosity_manager import Verbosity, VerbosityManager
from kerkov.core.session.verbosity_prompts import VERBOSITY_MARKER, VERBOSITY_PROMPTS
from kerkov.models.basic_models import Message


def _create_mock_cli(allow_autonomous: bool = False) -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()

    verbosity_manager = VerbosityManager(initial=Verbosity.STANDARD)
    approval_mode_manager = ApprovalModeManager(
        initial=ApprovalMode.NORMAL, allow_autonomous=allow_autonomous
    )
    cli.verbosity_manager = verbosity_manager
    cli.approval_mode_manager = approval_mode_manager
    cli.autonomy_manager = AutonomyManager(
        initial_level=2,
        allow_autonomous=allow_autonomous,
        verbosity_manager=verbosity_manager,
        approval_mode_manager=approval_mode_manager,
    )

    base_prompt = "You are a security assistant."
    system_content = (
        base_prompt + VERBOSITY_MARKER + VERBOSITY_PROMPTS[Verbosity.STANDARD]
    )
    cli.conversation_history = [
        Message(role="system", content=system_content),
    ]
    return cli


class SwitchAutonomyMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_numeric_switch(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("3")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.AGGRESSIVE)
        self.assertEqual(cli.verbosity_manager.current, Verbosity.AGGRESSIVE)
        self.assertEqual(cli.approval_mode_manager.current, ApprovalMode.PERMISSIVE)
        self.assertEqual(len(cli.conversation_history), 1)
        self.assertIn(
            VERBOSITY_PROMPTS[Verbosity.AGGRESSIVE],
            cli.conversation_history[0].content,
        )

    def test_valid_name_switch(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("guided")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.GUIDED)
        self.assertIn(
            VERBOSITY_PROMPTS[Verbosity.GUIDED],
            cli.conversation_history[0].content,
        )

    def test_system_prompt_old_fragment_removed(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("aggressive")
        self._run(msg.execute(cli))

        content = cli.conversation_history[0].content
        self.assertNotIn(
            VERBOSITY_PROMPTS[Verbosity.STANDARD],
            content,
        )

    def test_invalid_level_prints_error(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("invalid")
        self._run(msg.execute(cli))

        cli.console.print.assert_any_call(
            "[bold red]Error:[/bold red] Invalid autonomy level 'invalid'",
            style="bold red",
        )
        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.STANDARD)

    def test_level_4_without_allow_autonomous_prints_error(self):
        cli = _create_mock_cli(allow_autonomous=False)
        msg = SwitchAutonomyMessage("4")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.STANDARD)

    def test_level_4_with_allow_autonomous_succeeds(self):
        cli = _create_mock_cli(allow_autonomous=True)
        msg = SwitchAutonomyMessage("autonomous")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.AUTONOMOUS)
        self.assertEqual(cli.approval_mode_manager.current, ApprovalMode.YOLO)
        self.assertIn(
            VERBOSITY_PROMPTS[Verbosity.SILENT],
            cli.conversation_history[0].content,
        )

    def test_no_history_does_not_crash(self):
        cli = _create_mock_cli()
        cli.conversation_history = []
        msg = SwitchAutonomyMessage("3")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.AGGRESSIVE)


if __name__ == "__main__":
    unittest.main()
