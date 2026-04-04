"""Tests for SwitchAutonomyMessage."""

import asyncio
import unittest
from unittest.mock import MagicMock

from src.cli.autonomy_manager import AutonomyLevel, AutonomyManager
from src.cli.autonomy_prompts import AUTONOMY_MARKER, AUTONOMY_PROMPTS
from src.cli.messages.switch_autonomy_message import SwitchAutonomyMessage
from src.models.basic_models import Message


def _create_mock_cli(allow_autonomous: bool = False) -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.autonomy_manager = AutonomyManager(
        initial_level=2, allow_autonomous=allow_autonomous
    )
    # Provide a real conversation_history with a system message so the
    # in-place prompt update can be tested.
    base_prompt = "You are a security assistant."
    system_content = base_prompt + AUTONOMY_MARKER + AUTONOMY_PROMPTS[AutonomyLevel.STANDARD]
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
        # History is preserved, not cleared
        self.assertEqual(len(cli.conversation_history), 1)
        self.assertIn(
            AUTONOMY_PROMPTS[AutonomyLevel.AGGRESSIVE],
            cli.conversation_history[0].content,
        )

    def test_valid_name_switch(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("guided")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.GUIDED)
        self.assertIn(
            AUTONOMY_PROMPTS[AutonomyLevel.GUIDED],
            cli.conversation_history[0].content,
        )

    def test_system_prompt_old_fragment_removed(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("aggressive")
        self._run(msg.execute(cli))

        content = cli.conversation_history[0].content
        # The old STANDARD fragment should no longer be present
        self.assertNotIn(
            AUTONOMY_PROMPTS[AutonomyLevel.STANDARD],
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
        # Level unchanged
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
        self.assertIn(
            AUTONOMY_PROMPTS[AutonomyLevel.AUTONOMOUS],
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
