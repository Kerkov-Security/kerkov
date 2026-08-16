"""Tests for prompting and validating credentials during provider switches."""

import asyncio
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from prompt_toolkit.formatted_text import HTML, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from rich.console import Console

from kerkov.cli.kerkov_cli import KerkovCLI, _normalize_ollama_base_url
from kerkov.cli.provider_setup_extension import run_codex_oauth_browser_login
from kerkov.core.credentials import (
    GEMINI_API_KEY,
    CredentialInput,
    CredentialManager,
    CredentialSpec,
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
    cli._codex_auth_is_configured = MagicMock(return_value=True)
    cli.prompt_for_codex_login = AsyncMock(return_value=True)
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


def test_secret_prompt_escapes_dynamic_label_markup() -> None:
    label = "API token for '[prod]' <a&b>"
    spec = CredentialSpec(
        key="environment.TEST_API_TOKEN",
        env_var="TEST_API_TOKEN",
        label=label,
    )
    rendered = StringIO()
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = Console(file=rendered, color_system=None)
    cli.input_manager = MagicMock()
    cli.input_manager.is_watching_for_interrupt = False
    cli.input_manager.get_secret = AsyncMock(return_value="hidden-value")

    result = asyncio.run(cli.prompt_for_credential(spec))

    assert result == CredentialInput("hidden-value", persist=True)
    assert label in rendered.getvalue()
    prompt = cli.input_manager.get_secret.await_args.args[0]
    assert fragment_list_to_text(to_formatted_text(HTML(prompt))) == f"{label}: "


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


def test_codex_browser_login_runs_oauth_and_reports_success() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.console = MagicMock()
    cli.input_manager = SimpleNamespace(is_watching_for_interrupt=False)
    config = SimpleNamespace()

    with (
        patch(
            "kerkov.cli.provider_setup_extension.get_config",
            return_value=config,
        ),
        patch(
            "kerkov.cli.provider_setup_extension.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=(True, "Codex OAuth login complete."),
        ) as to_thread,
    ):
        result = asyncio.run(cli.prompt_for_codex_login())

    assert result is True
    to_thread.assert_awaited_once_with(run_codex_oauth_browser_login, config)
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "Sign in to Codex with your browser" in rendered
    assert "Codex OAuth login complete" in rendered


def test_codex_switch_runs_browser_login_when_auth_is_missing() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = True
    candidate.get_model_info.return_value = {"provider": "CodexProvider"}
    cli = _build_cli(candidate)
    cli._codex_auth_is_configured.return_value = False

    result = asyncio.run(cli.activate_provider_interactive("codex"))

    assert result is True
    cli.prompt_for_codex_login.assert_awaited_once_with()
    cli.provder_manager.build_provider.assert_called_once_with("codex")
    cli.provder_manager.set_provider.assert_called_once_with(candidate)


def test_cancelled_codex_browser_login_does_not_build_provider() -> None:
    cli = _build_cli(MagicMock())
    cli._codex_auth_is_configured.return_value = False
    cli.prompt_for_codex_login.return_value = False

    result = asyncio.run(cli.activate_provider_interactive("codex"))

    assert result is False
    cli.prompt_for_codex_login.assert_awaited_once_with()
    cli.provder_manager.build_provider.assert_not_called()
    cli.provder_manager.set_provider.assert_not_called()


def test_invalid_stored_codex_auth_runs_login_and_rebuilds_provider() -> None:
    stale_candidate = MagicMock()
    stale_candidate.validate_connection.return_value = False
    fresh_candidate = MagicMock()
    fresh_candidate.validate_connection.return_value = True
    fresh_candidate.get_model_info.return_value = {"provider": "CodexProvider"}
    cli = _build_cli(stale_candidate)
    cli.provder_manager.build_provider.side_effect = [
        stale_candidate,
        fresh_candidate,
    ]

    result = asyncio.run(cli.activate_provider_interactive("codex"))

    assert result is True
    assert cli.provder_manager.build_provider.call_args_list == [
        call("codex"),
        call("codex"),
    ]
    stale_candidate.validate_connection.assert_called_once_with()
    cli.prompt_for_codex_login.assert_awaited_once_with()
    fresh_candidate.validate_connection.assert_called_once_with()
    cli.provder_manager.set_provider.assert_called_once_with(fresh_candidate)


def test_invalid_codex_auth_retries_browser_login_only_once() -> None:
    stale_candidate = MagicMock()
    stale_candidate.validate_connection.return_value = False
    fresh_candidate = MagicMock()
    fresh_candidate.validate_connection.return_value = False
    cli = _build_cli(stale_candidate)
    cli.provder_manager.build_provider.side_effect = [
        stale_candidate,
        fresh_candidate,
    ]

    result = asyncio.run(cli.activate_provider_interactive("codex"))

    assert result is False
    cli.prompt_for_codex_login.assert_awaited_once_with()
    assert cli.provder_manager.build_provider.call_count == 2
    cli.provder_manager.set_provider.assert_not_called()


def test_ollama_url_is_prompted_validated_saved_and_activated() -> None:
    candidate = MagicMock()
    candidate.validate_connection.return_value = True
    candidate.get_model_info.return_value = {"provider": "OllamaProvider"}
    cli = _build_cli(candidate)
    cli.prompt_for_ollama_base_url = AsyncMock(
        return_value="https://ollama.example:11434"
    )

    with patch(
        "kerkov.cli.provider_setup_extension.save_user_setting",
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

    with patch(
        "kerkov.cli.provider_setup_extension.save_user_setting"
    ) as save_user_setting:
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
        "kerkov.cli.provider_setup_extension.get_config",
        return_value=SimpleNamespace(ollama_base=None),
    ):
        result = asyncio.run(cli.prompt_for_ollama_base_url())

    assert result == "https://ollama.example:11434"
    assert cli.input_manager.get_text.await_count == 2
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "must start with http:// or https://" in rendered
