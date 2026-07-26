"""Tests for runtime credential resolution and persistence."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

from kerkov.core.credentials import (
    GEMINI_API_KEY,
    TAVILY_API_KEY,
    AutoSecretStore,
    CredentialManager,
    KeyringSecretStore,
    LocalFileSecretStore,
    MemorySecretStore,
    credential_storage_description,
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


class UnavailableKeyringBackend:
    priority = 0

    def get_password(self, service: str, key: str) -> Optional[str]:
        raise AssertionError("Unavailable keyring should not be read")

    def set_password(self, service: str, key: str, value: str) -> None:
        raise AssertionError("Unavailable keyring should not be written")

    def delete_password(self, service: str, key: str) -> None:
        raise AssertionError("Unavailable keyring should not be written")


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
    assert result.storage == "keyring"
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


def test_headless_auto_store_persists_to_user_only_file(tmp_path: Path) -> None:
    credential_path = tmp_path / "kerkov" / "credentials.json"
    keyring_store = KeyringSecretStore(UnavailableKeyringBackend())
    manager = CredentialManager(
        environment={},
        persistent_store=AutoSecretStore(
            keyring_store,
            LocalFileSecretStore(credential_path),
        ),
    )

    result = manager.save(GEMINI_API_KEY, "persistent-secret")

    assert result.persisted is True
    assert result.storage == "local file"
    assert manager.source(GEMINI_API_KEY) == "session + local file"
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o700

    restarted_manager = CredentialManager(
        environment={},
        persistent_store=AutoSecretStore(
            KeyringSecretStore(UnavailableKeyringBackend()),
            LocalFileSecretStore(credential_path),
        ),
    )

    assert restarted_manager.get(GEMINI_API_KEY) == "persistent-secret"
    assert restarted_manager.source(GEMINI_API_KEY) == "local file"


def test_local_file_store_rejects_overly_broad_permissions(tmp_path: Path) -> None:
    credential_path = tmp_path / "credentials.json"
    credential_path.write_text('{"provider.gemini":"secret"}', encoding="utf-8")
    os.chmod(credential_path, 0o644)

    store = LocalFileSecretStore(credential_path)

    try:
        store.get(GEMINI_API_KEY.key)
    except PermissionError as exc:
        assert "0600" in str(exc)
    else:
        raise AssertionError("Expected broad credential-file permissions to fail")


def test_storage_description_does_not_include_secret_material() -> None:
    assert credential_storage_description("keyring") == "operating-system keyring"
    assert (
        credential_storage_description("local file")
        == "user-only local credential file"
    )


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
