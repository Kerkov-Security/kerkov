"""Provider-dependent commands remain usable before onboarding completes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.model_info_message import ModelInfoMessage
from kerkov.cli.messages.models_message import ModelsMessage
from kerkov.cli.messages.provider_info_message import ProviderInfoMessage


def _build_unconfigured_cli() -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.provder_manager.provider = None
    cli.activate_provider_interactive = AsyncMock()
    return cli


def _rendered_output(cli: MagicMock) -> str:
    return " ".join(str(call) for call in cli.console.print.call_args_list)


def test_provider_command_opens_picker_without_active_provider() -> None:
    cli = _build_unconfigured_cli()
    cli.select_provider_interactive = AsyncMock(return_value=None)

    asyncio.run(ProviderInfoMessage().execute(cli))

    cli.select_provider_interactive.assert_awaited_once_with()
    cli.activate_provider_interactive.assert_not_awaited()


def test_provider_command_activates_selected_provider() -> None:
    cli = _build_unconfigured_cli()
    cli.select_provider_interactive = AsyncMock(return_value="ollama")
    cli.current_provider_name.return_value = None

    asyncio.run(ProviderInfoMessage().execute(cli))

    cli.activate_provider_interactive.assert_awaited_once_with("ollama")


def test_provider_command_keeps_current_selection_and_shows_model() -> None:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.select_provider_interactive = AsyncMock(return_value="gemini")
    cli.activate_provider_interactive = AsyncMock()
    cli.current_provider_name.return_value = "gemini"
    cli.provder_manager.provider.get_model_info.return_value = {
        "provider": "GeminiProvider",
        "model": "gemini-2.5-flash",
    }

    asyncio.run(ProviderInfoMessage().execute(cli))

    cli.activate_provider_interactive.assert_not_awaited()
    output = _rendered_output(cli)
    assert "Current Provider" in output
    assert "Gemini" in output
    assert "gemini-2.5-flash" in output


def test_model_info_reports_setup_state_without_raising() -> None:
    cli = _build_unconfigured_cli()

    asyncio.run(ModelInfoMessage().execute(cli))

    output = _rendered_output(cli)
    assert "Current Model" in output
    assert "Not configured" in output
    assert "/provider <name>" in output


def test_models_reports_setup_state_without_contacting_provider() -> None:
    cli = _build_unconfigured_cli()

    asyncio.run(ModelsMessage().execute(cli))

    output = _rendered_output(cli)
    assert "Current Model" in output
    assert "Not configured" in output
    assert "/provider <name>" in output
