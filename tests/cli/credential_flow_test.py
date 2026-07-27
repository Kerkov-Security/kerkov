"""Tests for prompting and validating credentials during provider switches."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kerkov.cli.kerkov_cli import KerkovCLI, _normalize_ollama_base_url
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


def test_codex_switch_validates_catalog_and_displays_model_fallback() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = True
    candidate.model_selection_notice = (
        "Configured Codex model 'gpt-5-codex' is unavailable for this "
        "ChatGPT account; using 'gpt-5.4'."
    )
    candidate.get_model_info.return_value = {"provider": "CodexProvider"}
    cli = _build_cli(candidate)

    result = asyncio.run(cli.activate_provider_interactive("codex"))

    assert result is True
    cli.prompt_for_credential.assert_not_awaited()
    candidate.validate_connection.assert_called_once_with()
    cli.provder_manager.set_provider.assert_called_once_with(candidate)
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "using 'gpt-5.4'" in rendered


def test_ollama_url_is_prompted_validated_saved_and_activated() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = True
    candidate.get_model_info.return_value = {"provider": "OllamaProvider"}
    cli = _build_cli(candidate)
    cli.prompt_for_ollama_base_url = AsyncMock(
        return_value="https://ollama.example:11434"
    )

    with patch(
        "kerkov.cli.kerkov_cli.save_user_setting",
        return_value=Path("/tmp/kerkov-settings.json"),
    ) as save_user_setting:
        result = asyncio.run(cli.activate_provider_interactive("ollama"))

    assert result is True
    cli.provder_manager.build_provider.assert_called_once_with(
        "ollama",
        config_overrides={"ollama_base": "https://ollama.example:11434"},
    )
    candidate.validate_connection.assert_called_once_with()
    save_user_setting.assert_called_once_with(
        "OLLAMA_BASE",
        "https://ollama.example:11434",
    )
    cli.provder_manager.set_provider.assert_called_once_with(candidate)


def test_unreachable_ollama_url_is_not_saved_or_activated() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = False
    cli = _build_cli(candidate)
    cli.prompt_for_ollama_base_url = AsyncMock(
        return_value="http://unreachable.example:11434"
    )

    with patch("kerkov.cli.kerkov_cli.save_user_setting") as save_user_setting:
        result = asyncio.run(cli.activate_provider_interactive("ollama"))

    assert result is False
    save_user_setting.assert_not_called()
    cli.provder_manager.set_provider.assert_not_called()


def test_ollama_url_requires_plain_http_origin() -> None:
    assert (
        _normalize_ollama_base_url("HTTPS://ollama.example:11434/")
        == "https://ollama.example:11434"
    )

    with pytest.raises(ValueError, match="http:// or https://"):
        _normalize_ollama_base_url("ollama.example:11434")
    with pytest.raises(ValueError, match="username or password"):
        _normalize_ollama_base_url("https://user:password@ollama.example")
    with pytest.raises(ValueError, match="without a path"):
        _normalize_ollama_base_url("https://ollama.example/api")


def test_ollama_url_prompt_retries_invalid_input() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()
    cli.input_manager = MagicMock()
    cli.input_manager.is_watching_for_interrupt = False
    cli.input_manager.get_text = AsyncMock(
        side_effect=["not-a-url", "https://ollama.example:11434/"]
    )

    with patch(
        "kerkov.cli.kerkov_cli.get_config",
        return_value=SimpleNamespace(ollama_base=None),
    ):
        result = asyncio.run(cli.prompt_for_ollama_base_url())

    assert result == "https://ollama.example:11434"
    assert cli.input_manager.get_text.await_count == 2
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "must start with http:// or https://" in rendered
