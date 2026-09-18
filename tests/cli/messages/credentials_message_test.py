"""Tests for the credential-management slash command."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kerkov.cli.messages.credentials_message import CredentialsMessage
from kerkov.core.credentials import (
    ANTHROPIC_API_KEY,
    DEEPSEEK_API_KEY,
    GEMINI_API_KEY,
    TAVILY_API_KEY,
    ZAP_API_KEY,
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)


def _cli(environment=None, *, current_provider=None):
    credentials = CredentialManager(
        environment=environment or {},
        persistent_store=MemorySecretStore(),
    )
    return SimpleNamespace(
        credentials=credentials,
        prompt_for_credential=AsyncMock(
            return_value=CredentialInput("secret-value", persist=True)
        ),
        console=MagicMock(),
        current_provider_name=MagicMock(return_value=current_provider),
        provder_manager=MagicMock(),
        _reconfigure_context_for_provider=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("target", "spec"),
    [("gemini", GEMINI_API_KEY), ("deepseek", DEEPSEEK_API_KEY)],
)
def test_set_collects_secret_from_hidden_prompt_and_persists(target, spec) -> None:
    cli = _cli()

    asyncio.run(CredentialsMessage("set", target).execute(cli))

    cli.prompt_for_credential.assert_awaited_once_with(
        spec,
        persist=True,
    )
    assert cli.credentials.get(spec) == "secret-value"
    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "secret-value" not in rendered


def test_session_flag_does_not_write_persistent_store() -> None:
    persistent = MemorySecretStore()
    cli = _cli()
    cli.credentials = CredentialManager(
        environment={},
        persistent_store=persistent,
    )
    cli.prompt_for_credential.return_value = CredentialInput(
        "session-secret",
        persist=False,
    )

    asyncio.run(
        CredentialsMessage(
            "set",
            "gemini",
            session_only=True,
        ).execute(cli)
    )

    assert cli.credentials.get(GEMINI_API_KEY) == "session-secret"
    assert persistent.get(GEMINI_API_KEY.key) is None


def test_status_reports_source_without_value() -> None:
    cli = _cli(
        {
            "GOOGLE_API_KEY": "environment-secret",
            "DEEPSEEK_API_KEY": "deepseek-secret",
        }
    )

    asyncio.run(CredentialsMessage().execute(cli))

    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "gemini: configured (environment)" in rendered
    assert "deepseek: configured (environment)" in rendered
    assert "codex: not configured" in rendered
    assert "environment-secret" not in rendered
    assert "deepseek-secret" not in rendered


def test_remove_reports_environment_override_without_value() -> None:
    cli = _cli({"GOOGLE_API_KEY": "environment-secret"})

    asyncio.run(CredentialsMessage("remove", "gemini").execute(cli))

    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "GOOGLE_API_KEY" in rendered
    assert "environment-secret" not in rendered


def test_remove_disconnects_active_provider() -> None:
    cli = _cli(current_provider="gemini")
    cli.credentials.save(GEMINI_API_KEY, "gemini-secret")

    asyncio.run(CredentialsMessage("remove", "gemini").execute(cli))

    assert cli.credentials.get(GEMINI_API_KEY) is None
    cli.provder_manager.clear_provider.assert_called_once_with()
    cli._reconfigure_context_for_provider.assert_awaited_once_with()


def test_remove_alias_disconnects_canonical_provider() -> None:
    cli = _cli(current_provider="anthropic")
    cli.credentials.save(ANTHROPIC_API_KEY, "anthropic-secret")

    asyncio.run(CredentialsMessage("remove", "claude").execute(cli))

    assert cli.credentials.get(ANTHROPIC_API_KEY) is None
    cli.provder_manager.clear_provider.assert_called_once_with()


def test_remove_tool_credential_keeps_active_provider() -> None:
    cli = _cli(current_provider="gemini")
    cli.credentials.save(TAVILY_API_KEY, "tavily-secret")

    asyncio.run(CredentialsMessage("remove", "tavily").execute(cli))

    cli.provder_manager.clear_provider.assert_not_called()
    cli._reconfigure_context_for_provider.assert_not_awaited()


def test_zap_alias_uses_managed_credential() -> None:
    cli = _cli()

    asyncio.run(CredentialsMessage("set", "zap").execute(cli))

    cli.prompt_for_credential.assert_awaited_once_with(
        ZAP_API_KEY,
        persist=True,
    )
    assert cli.credentials.get(ZAP_API_KEY) == "secret-value"


def test_status_lists_zap_without_exposing_value() -> None:
    cli = _cli({"ZAP_API_KEY": "zap-environment-secret"})

    asyncio.run(CredentialsMessage().execute(cli))

    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "zap: configured (environment)" in rendered
    assert "zap-environment-secret" not in rendered
