"""Tests for SwitchModelMessage."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.switch_model_message import SwitchModelMessage
from kerkov.providers.codex import CodexModelNotAvailableError


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
    cli.provder_manager.agent_routing_enabled = False
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
        cli.provder_manager.disable_agent_routing.assert_called_once_with()
        cli._reconfigure_context_for_provider.assert_awaited_once()

    def test_auto_enables_delegate_routing_without_changing_main_model(self):
        cli = _create_mock_cli()

        _run(SwitchModelMessage("auto").execute(cli))

        cli.provder_manager.enable_agent_routing.assert_called_once_with()
        cli.provder_manager.disable_agent_routing.assert_not_called()
        cli.provder_manager.provider.set_model.assert_not_called()
        cli.provder_manager.provider.list_models.assert_not_called()
        cli._reconfigure_context_for_provider.assert_not_awaited()
        output = " ".join(str(call) for call in cli.console.print.call_args_list)
        self.assertIn("future delegates", output)
        self.assertIn("openrouter/auto", output)

    def test_invalid_routing_configuration_is_reported(self):
        cli = _create_mock_cli()
        cli.provder_manager.enable_agent_routing.side_effect = ValueError(
            "AGENT_ROUTING_SMALL_MODEL must be configured"
        )

        _run(SwitchModelMessage("auto").execute(cli))

        cli.provder_manager.provider.set_model.assert_not_called()
        cli.provder_manager.disable_agent_routing.assert_not_called()
        cli._reconfigure_context_for_provider.assert_not_awaited()
        output = " ".join(str(call) for call in cli.console.print.call_args_list)
        self.assertIn("AGENT_ROUTING_SMALL_MODEL must be configured", output)

    def test_openrouter_auto_is_a_concrete_model_and_disables_routing(self):
        cli = _create_mock_cli()
        cli.provder_manager.agent_routing_enabled = True

        _run(SwitchModelMessage("openrouter/auto").execute(cli))

        cli.provder_manager.provider.set_model.assert_called_once_with(
            "openrouter/auto"
        )
        cli.provder_manager.disable_agent_routing.assert_called_once_with()
        cli.provder_manager.enable_agent_routing.assert_not_called()

    def test_prints_error_when_provider_is_missing(self):
        cli = MagicMock()
        cli.console = MagicMock()
        cli.provder_manager.provider = None

        msg = SwitchModelMessage("openrouter/auto")
        _run(msg.execute(cli))

        cli.console.print.assert_any_call(
            "[bold yellow]No provider is currently configured.[/bold yellow]"
        )

    def test_reports_unavailable_codex_model_without_reconfiguring(self):
        cli = _create_mock_cli()
        cli.provder_manager.provider.set_model.side_effect = (
            CodexModelNotAvailableError(
                "gpt-5-codex",
                ["gpt-5.4", "gpt-5.4-mini"],
            )
        )

        _run(SwitchModelMessage("gpt-5-codex").execute(cli))

        cli._reconfigure_context_for_provider.assert_not_awaited()
        cli.provder_manager.disable_agent_routing.assert_not_called()
        output = " ".join(
            str(call.args[0]) for call in cli.console.print.call_args_list if call.args
        )
        self.assertIn("/models", output)
        self.assertIn("gpt-5.4-mini", output)


if __name__ == "__main__":
    unittest.main()
