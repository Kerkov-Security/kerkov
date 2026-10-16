import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.switch_mode_message import SwitchModeMessage
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType


def _cli(order):
    mode_manager = MagicMock()
    mode_manager.parse_mode_string.return_value = ModeType.OFFENSIVE
    mode_manager.current_mode = ModeManager().get_mode(ModeType.AUDIT)
    mode_manager.switch_mode.return_value = ModeManager().get_mode(ModeType.OFFENSIVE)
    return SimpleNamespace(
        mode_manager=mode_manager,
        console=MagicMock(),
        settle_agents_for_conversation_switch=AsyncMock(
            side_effect=lambda: order.append("settle")
        ),
        clear_history=MagicMock(side_effect=lambda: order.append("clear")),
    )


class SwitchModeMessageTest(unittest.TestCase):

    def test_mode_switch_settles_agents_before_wiping_history(self):
        """A /mode switch tears down the conversation like /clear, so agents
        must be settled first or their reports land in the new session."""
        order: list = []
        cli = _cli(order)

        asyncio.run(SwitchModeMessage("offensive").execute(cli))

        self.assertEqual(order, ["settle", "clear"])
        cli.mode_manager.switch_mode.assert_called_once_with(ModeType.OFFENSIVE)

    def test_invalid_mode_neither_settles_nor_clears(self):
        order: list = []
        cli = _cli(order)
        cli.mode_manager.parse_mode_string.return_value = None

        asyncio.run(SwitchModeMessage("bogus").execute(cli))

        self.assertEqual(order, [])
        cli.clear_history.assert_not_called()
        cli.settle_agents_for_conversation_switch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
