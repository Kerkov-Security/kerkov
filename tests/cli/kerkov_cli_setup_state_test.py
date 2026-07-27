"""Tests for the CLI's provider-free setup state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.kerkov_cli import KerkovCLI


class FailingMessage:
    async def execute(self, cli) -> None:
        raise RuntimeError("simulated command bug")


def _rendered_output(cli: KerkovCLI) -> str:
    return " ".join(str(call) for call in cli.console.print.call_args_list)


def test_command_failure_is_contained_by_interactive_boundary() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()

    asyncio.run(cli._execute_user_message_safely(FailingMessage()))

    output = _rendered_output(cli)
    assert "Command failed (RuntimeError)" in output
    assert "still running" in output
    assert "simulated command bug" not in output


def test_setup_guidance_explains_available_commands() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()

    cli._display_provider_setup_guidance()

    output = _rendered_output(cli)
    assert "setup mode" in output
    assert "/providers" in output
    assert "/provider <name>" in output
    assert "/auth" in output


def test_interactive_provider_info_survives_cancelled_onboarding() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()
    cli.provder_manager = SimpleNamespace(provider=None)
    cli.config = SimpleNamespace(llm_provider="gemini")
    cli.mode_manager = SimpleNamespace(
        current_mode=SimpleNamespace(color="cyan", name="Audit")
    )
    cli.input_manager = SimpleNamespace(
        get_line=AsyncMock(side_effect=["/provider", EOFError()])
    )
    cli.activate_provider_interactive = AsyncMock(return_value=False)
    cli._display_welcome = MagicMock()
    cli._display_provider_setup_guidance = MagicMock()
    cli.print_resume_command = MagicMock()

    with patch("kerkov.cli.kerkov_cli.console"):
        asyncio.run(cli.run())

    cli.activate_provider_interactive.assert_awaited_once_with("gemini")
    cli._display_provider_setup_guidance.assert_called_once_with()
    assert cli.input_manager.get_line.await_count == 2
    assert "Current Provider" in _rendered_output(cli)
    cli.print_resume_command.assert_called_once_with()
