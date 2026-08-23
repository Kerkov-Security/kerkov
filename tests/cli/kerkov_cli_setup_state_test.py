"""Tests for the CLI's provider-free setup state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_toolkit.formatted_text import to_formatted_text

from kerkov.cli.kerkov_cli import SETUP_LATER_PROVIDER, KerkovCLI


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


def test_blank_chat_submission_is_a_no_op() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.provder_manager = SimpleNamespace(provider=MagicMock())
    cli._apply_system_prompt_if_possible = MagicMock()
    cli._ensure_compactor = AsyncMock()
    cli.engine = SimpleNamespace(run_loop=AsyncMock())
    cli.conversation_history = []

    asyncio.run(cli.get_response("   "))

    cli._apply_system_prompt_if_possible.assert_not_called()
    cli._ensure_compactor.assert_not_awaited()
    cli.engine.run_loop.assert_not_awaited()
    assert cli.conversation_history == []


def test_setup_guidance_explains_available_commands() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()

    cli._display_provider_setup_guidance()

    output = _rendered_output(cli)
    assert "setup mode" in output
    assert "/provider" in output
    assert "picker" in output
    assert "/provider <name>" in output
    assert "/auth" in output


def test_interactive_provider_info_survives_cancelled_onboarding() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()
    cli.provder_manager = SimpleNamespace(provider=None)
    cli.credentials = MagicMock()
    cli.credentials.get.return_value = None
    cli.config = SimpleNamespace(llm_provider="gemini")
    cli.mode_manager = SimpleNamespace(
        current_mode=SimpleNamespace(color="cyan", name="Audit")
    )
    cli.input_manager = SimpleNamespace(
        choose=AsyncMock(side_effect=["gemini", None]),
        get_line=AsyncMock(side_effect=["/provider", EOFError()]),
    )
    cli.activate_provider_interactive = AsyncMock(return_value=False)
    cli._display_welcome = MagicMock()
    cli._display_provider_setup_guidance = MagicMock()
    cli.print_resume_command = MagicMock()
    cli.shutdown_agents = AsyncMock()

    with patch("kerkov.cli.kerkov_cli.console"):
        asyncio.run(cli.run())

    assert cli.input_manager.choose.await_count == 2
    cli.activate_provider_interactive.assert_awaited_once_with(
        "gemini",
        validate_existing=True,
    )
    cli._display_provider_setup_guidance.assert_called_once_with()
    assert cli.input_manager.get_line.await_count == 2
    cli.print_resume_command.assert_called_once_with()


def test_provider_picker_labels_options_and_allows_setup_later() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.credentials = MagicMock()
    cli.credentials.get.return_value = None
    cli.config = SimpleNamespace(llm_provider="gemini")
    cli.provder_manager = SimpleNamespace(provider=None)
    cli.input_manager = SimpleNamespace(
        choose=AsyncMock(return_value=SETUP_LATER_PROVIDER)
    )

    selected = asyncio.run(cli._select_startup_provider())

    assert selected is None
    choose_call = cli.input_manager.choose.await_args
    options = dict(choose_call.args[1])
    assert options["gemini"] == "Gemini — API key required"
    assert options["openrouter"] == "OpenRouter — API key required"
    assert options["ollama"] == "Ollama — server URL required; no API key"
    assert options[SETUP_LATER_PROVIDER].endswith("setup mode")


def test_provider_picker_preselects_active_provider_and_shows_model() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    provider = MagicMock()
    provider.get_model_info.return_value = {
        "provider": "OpenRouterProvider",
        "model": "openrouter/auto",
    }
    cli.provder_manager = SimpleNamespace(provider=provider)
    cli.credentials = MagicMock()
    cli.credentials.get.return_value = None
    cli.config = SimpleNamespace(llm_provider="gemini")
    cli.input_manager = SimpleNamespace(choose=AsyncMock(return_value=None))

    selected = asyncio.run(cli.select_provider_interactive())

    assert selected is None
    choose_call = cli.input_manager.choose.await_args
    options = dict(choose_call.args[1])
    active_label = to_formatted_text(options["openrouter"])
    assert active_label == [("class:active-choice", "OpenRouter — openrouter/auto")]
    assert choose_call.kwargs["default"] == "openrouter"
    assert SETUP_LATER_PROVIDER not in options


def test_unreachable_configured_provider_falls_back_to_setup_mode() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    provider = MagicMock()
    provider.validate_connection.return_value = False
    cli.provder_manager = MagicMock()
    cli.provder_manager.provider = provider
    cli.config = SimpleNamespace(llm_provider="ollama")
    cli.console = MagicMock()

    with patch(
        "kerkov.cli.provider_setup_extension.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=False,
    ) as to_thread:
        asyncio.run(cli._initialize_provider())

    to_thread.assert_awaited_once_with(provider.validate_connection)
    cli.provder_manager.clear_provider.assert_called_once_with()
