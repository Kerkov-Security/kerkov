"""Tests for runtime credential resolution and persistence."""

from __future__ import annotations

from typing import Optional

from kerkov.core.credentials import (
    GEMINI_API_KEY,
    TAVILY_API_KEY,
    CredentialManager,
    MemorySecretStore,
    credential_spec_from_name,
    provider_credential_spec,
    sanitized_subprocess_environment,
)


class FailingSecretStore:
    def get(self, key: str) -> Optional[str]:
        return None

    def set(self, key: str, value: str) -> None:
        raise RuntimeError(f"could not store {value}")

    def delete(self, key: str) -> None:
        raise RuntimeError("no secure backend")


def test_provider_and_tool_aliases_resolve_to_specs() -> None:
    assert provider_credential_spec("google") is GEMINI_API_KEY
    assert provider_credential_spec("ollama") is None
    assert credential_spec_from_name("web-search") is TAVILY_API_KEY


def test_resolution_prefers_session_then_environment_then_keyring() -> None:
    persistent = MemorySecretStore()
    persistent.set(GEMINI_API_KEY.key, "keyring-value")
    manager = CredentialManager(
        environment={"GOOGLE_API_KEY": "environment-value"},
        persistent_store=persistent,
    )

    assert manager.get(GEMINI_API_KEY) == "environment-value"
    assert manager.source(GEMINI_API_KEY) == "environment"

    manager.set_session(GEMINI_API_KEY, "session-value")

    assert manager.get(GEMINI_API_KEY) == "session-value"
    assert manager.source(GEMINI_API_KEY) == "session"


def test_keyring_used_when_no_session_or_environment_value() -> None:
    persistent = MemorySecretStore()
    persistent.set(GEMINI_API_KEY.key, "keyring-value")
    manager = CredentialManager(environment={}, persistent_store=persistent)

    assert manager.get(GEMINI_API_KEY) == "keyring-value"
    assert manager.source(GEMINI_API_KEY) == "keyring"


def test_save_persists_validated_value() -> None:
    persistent = MemorySecretStore()
    manager = CredentialManager(environment={}, persistent_store=persistent)

    result = manager.save(GEMINI_API_KEY, "  secret-value  ")

    assert result.persisted is True
    assert result.warning is None
    assert manager.get(GEMINI_API_KEY) == "secret-value"
    assert manager.source(GEMINI_API_KEY) == "session + keyring"
    assert persistent.get(GEMINI_API_KEY.key) == "secret-value"


def test_save_falls_back_to_session_when_keyring_unavailable() -> None:
    manager = CredentialManager(
        environment={},
        persistent_store=FailingSecretStore(),
    )

    result = manager.save(GEMINI_API_KEY, "secret-value")

    assert result.persisted is False
    assert "session only" in (result.warning or "")
    assert "secret-value" not in (result.warning or "")
    assert manager.get(GEMINI_API_KEY) == "secret-value"


def test_delete_warns_when_environment_still_supplies_value() -> None:
    manager = CredentialManager(
        environment={"GOOGLE_API_KEY": "environment-value"},
        persistent_store=MemorySecretStore(),
    )

    warning = manager.delete(GEMINI_API_KEY)

    assert "GOOGLE_API_KEY" in (warning or "")
    assert "environment-value" not in (warning or "")


def test_delete_warns_when_persistent_removal_cannot_be_confirmed() -> None:
    manager = CredentialManager(
        environment={},
        persistent_store=FailingSecretStore(),
    )

    warning = manager.delete(GEMINI_API_KEY)

    assert "could not be confirmed as removed" in (warning or "")


def test_subprocess_environment_removes_managed_secrets() -> None:
    child_env = sanitized_subprocess_environment(
        {
            "PATH": "/usr/bin",
            "GOOGLE_API_KEY": "gemini-secret",
            "SEARCH_API_KEY": "tavily-secret",
            "KERKOV_WEB_ACCESS_TOKEN": "web-secret",
            "HTB_API_TOKEN": "still-required-by-current-skill-runner",
        }
    )

    assert child_env == {
        "PATH": "/usr/bin",
        "HTB_API_TOKEN": "still-required-by-current-skill-runner",
    }
