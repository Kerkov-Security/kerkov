"""Tests for the credential-management slash command."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.messages.credentials_message import CredentialsMessage
from kerkov.core.credentials import (
    GEMINI_API_KEY,
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)


def _cli(environment=None):
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
    )


def test_set_collects_secret_from_hidden_prompt_and_persists() -> None:
    cli = _cli()

    asyncio.run(CredentialsMessage("set", "gemini").execute(cli))

    cli.prompt_for_credential.assert_awaited_once_with(
        GEMINI_API_KEY,
        persist=True,
    )
    assert cli.credentials.get(GEMINI_API_KEY) == "secret-value"
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
    cli = _cli({"GOOGLE_API_KEY": "environment-secret"})

    asyncio.run(CredentialsMessage().execute(cli))

    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "gemini: configured (environment)" in rendered
    assert "environment-secret" not in rendered


def test_remove_reports_environment_override_without_value() -> None:
    cli = _cli({"GOOGLE_API_KEY": "environment-secret"})

    asyncio.run(CredentialsMessage("remove", "gemini").execute(cli))

    rendered = " ".join(str(call) for call in cli.console.print.call_args_list)
    assert "GOOGLE_API_KEY" in rendered
    assert "environment-secret" not in rendered
