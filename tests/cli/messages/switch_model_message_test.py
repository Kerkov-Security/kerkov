"""Tests for SwitchModelMessage."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.switch_model_message import SwitchModelMessage


def _run(coro):
    return asyncio.run(coro)


def _create_mock_cli() -> MagicMock:
    provider = MagicMock()
    provider.model = "openrouter/auto"
    provider.list_models.side_effect = AssertionError(
        "/model should not fetch remote model lists"
    )

    cli = MagicMock()
    cli.console = MagicMock()
    cli.provder_manager.provider = provider
    cli._reconfigure_context_for_provider = AsyncMock()
    return cli


class SwitchModelMessageTest(unittest.TestCase):

    def test_switches_model_without_listing_remote_models(self):
        cli = _create_mock_cli()
        msg = SwitchModelMessage("anthropic/claude-opus-4.7")

        _run(msg.execute(cli))

        cli.provder_manager.provider.set_model.assert_called_once_with(
            "anthropic/claude-opus-4.7"
        )
        cli.provder_manager.provider.list_models.assert_not_called()
        cli._reconfigure_context_for_provider.assert_awaited_once()

    def test_prints_error_when_provider_is_missing(self):
        cli = MagicMock()
        cli.console = MagicMock()
        cli.provder_manager.provider = None

        msg = SwitchModelMessage("openrouter/auto")
        _run(msg.execute(cli))

        cli.console.print.assert_any_call(
            "[bold yellow]No provider is currently configured.[/bold yellow]"
        )


if __name__ == "__main__":
    unittest.main()
