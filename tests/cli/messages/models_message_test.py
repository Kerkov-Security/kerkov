"""Tests for ModelsMessage."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.models_message import ModelsMessage


def _run(coro):
    return asyncio.run(coro)


def _create_mock_cli() -> MagicMock:
    provider = MagicMock()
    provider.get_model_info.return_value = {
        "provider": "OpenRouterProvider",
        "model": "openrouter/auto",
    }
    provider.list_models.return_value = [
        "openrouter/auto",
        "anthropic/claude-opus-4.7",
    ]

    cli = MagicMock()
    cli.console = MagicMock()
    cli.provder_manager.provider = provider
    cli.input_manager.choose = AsyncMock(return_value=None)
    cli._reconfigure_context_for_provider = AsyncMock()
    return cli


def _printed_messages(cli: MagicMock) -> list[str]:
    return [
        str(call.args[0]) if call.args else ""
        for call in cli.console.print.call_args_list
    ]


def _printed_contains(cli: MagicMock, fragment: str) -> bool:
    return any(fragment in message for message in _printed_messages(cli))


class ModelsMessageTest(unittest.TestCase):

    def test_opens_picker_with_current_model_highlighted(self):
        cli = _create_mock_cli()

        _run(ModelsMessage().execute(cli))

        cli.provder_manager.provider.list_models.assert_called_once()
        choose_call = cli.input_manager.choose.await_args
        self.assertEqual(choose_call.args[0], "Select a model for OpenRouter")
        options = dict(choose_call.args[1])
        self.assertEqual(
            options["openrouter/auto"],
            "openrouter/auto — current",
        )
        self.assertEqual(
            options["anthropic/claude-opus-4.7"],
            "anthropic/claude-opus-4.7",
        )
        self.assertEqual(choose_call.kwargs["default"], "openrouter/auto")

    def test_switches_to_selected_model(self):
        cli = _create_mock_cli()
        cli.input_manager.choose.return_value = "anthropic/claude-opus-4.7"

        _run(ModelsMessage().execute(cli))

        cli.provder_manager.provider.set_model.assert_called_once_with(
            "anthropic/claude-opus-4.7"
        )
        cli._reconfigure_context_for_provider.assert_awaited_once()

    def test_times_out_when_provider_model_list_hangs(self):
        cli = _create_mock_cli()

        async def slow_list_call(_func):
            await asyncio.sleep(1)

        with (
            patch(
                "kerkov.cli.messages.model_info_message.MODEL_LIST_TIMEOUT_SECONDS",
                0.01,
            ),
            patch(
                "kerkov.cli.messages.model_info_message.asyncio.to_thread",
                side_effect=slow_list_call,
            ),
        ):
            _run(ModelsMessage().execute(cli))

        self.assertTrue(_printed_contains(cli, "Timed out listing models"))
        self.assertTrue(_printed_contains(cli, "/model <name>"))

    def test_reports_setup_guidance_when_provider_is_missing(self):
        cli = _create_mock_cli()
        cli.provder_manager.provider = None

        _run(ModelsMessage().execute(cli))

        self.assertTrue(_printed_contains(cli, "Current Model"))
        self.assertTrue(_printed_contains(cli, "Not configured"))
        self.assertTrue(_printed_contains(cli, "/provider <name>"))


if __name__ == "__main__":
    unittest.main()
