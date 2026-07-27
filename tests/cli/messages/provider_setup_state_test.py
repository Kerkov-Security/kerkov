"""Provider-dependent commands remain usable before onboarding completes."""

import asyncio
from unittest.mock import MagicMock

from kerkov.cli.messages.model_info_message import ModelInfoMessage
from kerkov.cli.messages.models_message import ModelsMessage
from kerkov.cli.messages.provider_info_message import ProviderInfoMessage


def _build_unconfigured_cli() -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.provder_manager.provider = None
    return cli


def _rendered_output(cli: MagicMock) -> str:
    return " ".join(str(call) for call in cli.console.print.call_args_list)


def test_provider_info_reports_setup_state_without_raising() -> None:
    cli = _build_unconfigured_cli()

    asyncio.run(ProviderInfoMessage().execute(cli))

    output = _rendered_output(cli)
    assert "Current Provider" in output
    assert "Not configured" in output
    assert "/provider <name>" in output


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
    assert "Available Models" in output
    assert "Provider not configured" in output
    assert "/provider <name>" in output
