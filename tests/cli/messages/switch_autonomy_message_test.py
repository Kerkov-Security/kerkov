"""Tests for SwitchAutonomyMessage."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from src.cli.autonomy_manager import AutonomyLevel, AutonomyManager
from src.cli.messages.switch_autonomy_message import SwitchAutonomyMessage


def _create_mock_cli(allow_autonomous: bool = False) -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.autonomy_manager = AutonomyManager(
        initial_level=2, allow_autonomous=allow_autonomous
    )
    return cli


class SwitchAutonomyMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_numeric_switch(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("3")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.AGGRESSIVE)
        cli.clear_history.assert_called_once()

    def test_valid_name_switch(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("guided")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.GUIDED)
        cli.clear_history.assert_called_once()

    def test_invalid_level_prints_error(self):
        cli = _create_mock_cli()
        msg = SwitchAutonomyMessage("invalid")
        self._run(msg.execute(cli))

        cli.console.print.assert_any_call(
            "[bold red]Error:[/bold red] Invalid autonomy level 'invalid'",
            style="bold red",
        )
        cli.clear_history.assert_not_called()

    def test_level_4_without_allow_autonomous_prints_error(self):
        cli = _create_mock_cli(allow_autonomous=False)
        msg = SwitchAutonomyMessage("4")
        self._run(msg.execute(cli))

        # Should not have switched
        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.STANDARD)
        cli.clear_history.assert_not_called()

    def test_level_4_with_allow_autonomous_succeeds(self):
        cli = _create_mock_cli(allow_autonomous=True)
        msg = SwitchAutonomyMessage("autonomous")
        self._run(msg.execute(cli))

        self.assertEqual(cli.autonomy_manager.current_level, AutonomyLevel.AUTONOMOUS)
        cli.clear_history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
