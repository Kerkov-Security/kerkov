import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.clear_message import ClearMessage


class ClearMessageTest(unittest.TestCase):
    def test_clear_settles_outstanding_agents_before_wiping_history(self):
        """Otherwise their reports drain into the conversation just cleared."""
        order = []
        cli = SimpleNamespace(
            settle_agents_for_conversation_switch=AsyncMock(
                side_effect=lambda: order.append("settle")
            ),
            clear_history=MagicMock(side_effect=lambda: order.append("clear")),
        )

        asyncio.run(ClearMessage().execute(cli))

        self.assertEqual(order, ["settle", "clear"])


if __name__ == "__main__":
    unittest.main()
