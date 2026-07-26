"""Tests for prompting and validating credentials during provider switches."""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.core.credentials import (
    GEMINI_API_KEY,
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)


def _build_cli(candidate) -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.credentials = CredentialManager(
        environment={},
        persistent_store=MemorySecretStore(),
    )
    cli.prompt_for_credential = AsyncMock(
        return_value=CredentialInput("gemini-secret", persist=True)
    )
    cli.provder_manager = MagicMock()
    cli.provder_manager.build_provider.return_value = candidate
    cli.console = MagicMock()
    cli._reconfigure_context_for_provider = AsyncMock()
    return cli


def test_secret_prompt_suspends_active_status_renderer() -> None:
    events = []

    @contextmanager
    def suspended_status():
        events.append("status-stopped")
        try:
            yield
        finally:
            events.append("status-restarted")

    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()
    cli.output_sink = MagicMock()
    cli.output_sink.suspend_status.side_effect = suspended_status
    cli.input_manager = MagicMock()
    cli.input_manager.is_watching_for_interrupt = False

    async def get_secret(prompt):
        events.append("secret-prompt")
        return "hidden-value"

    cli.input_manager.get_secret = AsyncMock(side_effect=get_secret)

    result = asyncio.run(cli.prompt_for_credential(GEMINI_API_KEY))

    assert result == CredentialInput("hidden-value", persist=True)
    assert events == ["status-stopped", "secret-prompt", "status-restarted"]


def test_missing_gemini_key_is_prompted_validated_and_activated() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = True
    candidate.get_model_info.return_value = {"provider": "GeminiProvider"}
    cli = _build_cli(candidate)

    result = asyncio.run(cli.activate_provider_interactive("gemini"))

    assert result is True
    cli.prompt_for_credential.assert_awaited_once_with(GEMINI_API_KEY, True)
    candidate.validate_connection.assert_called_once_with()
    cli.provder_manager.set_provider.assert_called_once_with(candidate)
    assert cli.credentials.get(GEMINI_API_KEY) == "gemini-secret"
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "gemini-secret" not in rendered


def test_invalid_prompted_key_is_discarded_and_not_activated() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = False
    cli = _build_cli(candidate)

    result = asyncio.run(cli.activate_provider_interactive("gemini"))

    assert result is False
    cli.provder_manager.set_provider.assert_not_called()
    assert cli.credentials.get(GEMINI_API_KEY) is None


def test_existing_credential_switches_without_prompting_again() -> None:
    candidate = MagicMock()
    candidate.get_model_info.return_value = {"provider": "GeminiProvider"}
    cli = _build_cli(candidate)
    cli.credentials.set_session(GEMINI_API_KEY, "existing-secret")

    result = asyncio.run(cli.activate_provider_interactive("gemini"))

    assert result is True
    cli.prompt_for_credential.assert_not_awaited()
    candidate.validate_connection.assert_not_called()
    cli.provder_manager.set_provider.assert_called_once_with(candidate)
