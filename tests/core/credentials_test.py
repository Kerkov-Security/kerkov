"""Tests for runtime credential resolution and persistence."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

from kerkov.core import credentials as credentials_module
from kerkov.core.credentials import (
    GEMINI_API_KEY,
    KEYRING_INDETERMINATE_SUFFIX,
    KEYRING_NOTICE_AFTER_SECONDS,
    TAVILY_API_KEY,
    AutoSecretStore,
    CredentialInput,
    CredentialManager,
    KeyringSecretStore,
    KeyringUnavailableError,
    LocalFileSecretStore,
    MemorySecretStore,
    credential_spec_for_env_var,
    credential_spec_from_name,
    credential_storage_description,
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


class HangingKeyringBackend:
    """A keyring whose calls never return, like a locked collection whose
    unlock prompt can never be completed."""

    priority = 5

    def __init__(self) -> None:
        self.released = threading.Event()
        self.calls = 0

    def _block(self) -> None:
        self.calls += 1
        # Bounded so a broken timeout fails the test instead of wedging it.
        self.released.wait(30)

    def get_password(self, service: str, key: str) -> Optional[str]:
        self._block()
        return "never-delivered"

    def set_password(self, service: str, key: str, value: str) -> None:
        self._block()

    def delete_password(self, service: str, key: str) -> None:
        self._block()


class DeferredKeyringBackend:
    """A keyring that is slow rather than wedged.

    Its writes block until the test releases them and then actually land, which
    is what lets an abandoned write reappear after a newer value was saved
    elsewhere.
    """

    priority = 5

    def __init__(self) -> None:
        self.released = threading.Event()
        self.applied = threading.Event()
        self.store: dict[str, str] = {}

    def get_password(self, service: str, key: str) -> Optional[str]:
        return self.store.get(key)

    def set_password(self, service: str, key: str, value: str) -> None:
        self.released.wait(30)
        self.store[key] = value
        self.applied.set()

    def delete_password(self, service: str, key: str) -> None:
        self.store.pop(key, None)


def test_provider_and_tool_aliases_resolve_to_specs() -> None:
    assert provider_credential_spec("google") is GEMINI_API_KEY
    assert provider_credential_spec("ollama") is None
    assert credential_spec_from_name("web-search") is TAVILY_API_KEY


def test_profile_environment_variable_resolves_to_stable_spec() -> None:
    spec = credential_spec_for_env_var("HTB_API_TOKEN")

    assert spec.key == "environment.HTB_API_TOKEN"
    assert spec.env_var == "HTB_API_TOKEN"
    assert credential_spec_from_name("HTB_API_TOKEN") == spec
    assert credential_spec_from_name("not-a-profile-variable") is None


def test_declared_runtime_credential_is_prompted_and_saved() -> None:
    persistent = MemorySecretStore()
    manager = CredentialManager(environment={}, persistent_store=persistent)
    prompt = AsyncMock(return_value=CredentialInput("profile-secret", persist=True))

    result = asyncio.run(
        manager.ensure_environment_credentials(
            ["HTB_API_TOKEN"],
            prompt,
            context="skill 'htb-machine-setup'",
        )
    )

    assert result.ready is True
    prompt.assert_awaited_once()
    spec = credential_spec_for_env_var("HTB_API_TOKEN")
    assert persistent.get(spec.key) == "profile-secret"


def test_declared_runtime_credential_reports_missing_without_prompt() -> None:
    manager = CredentialManager(
        environment={},
        persistent_store=MemorySecretStore(),
    )

    result = asyncio.run(
        manager.ensure_environment_credentials(
            ["HTB_API_TOKEN"],
            None,
            context="skill 'htb-machine-setup'",
        )
    )

    assert result.ready is False
    assert result.missing is not None
    assert result.missing.env_var == "HTB_API_TOKEN"


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


def test_local_file_store_preserves_existing_custom_parent_permissions(
    tmp_path: Path,
) -> None:
    credential_path = tmp_path / "shared" / "credentials.json"
    credential_path.parent.mkdir()
    if os.name != "nt":
        os.chmod(credential_path.parent, 0o755)

    LocalFileSecretStore(credential_path).set(GEMINI_API_KEY.key, "secret")

    if os.name != "nt":
        assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o755


def test_local_file_store_hardens_existing_managed_parent_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credential_path = tmp_path / ".kerkov" / "credentials.json"
    credential_path.parent.mkdir()
    monkeypatch.setattr(
        credentials_module,
        "DEFAULT_CREDENTIAL_FILE",
        str(credential_path),
    )
    if os.name != "nt":
        os.chmod(credential_path.parent, 0o755)

    LocalFileSecretStore(credential_path).set(GEMINI_API_KEY.key, "secret")

    if os.name != "nt":
        assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o700


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


def test_manager_warns_when_credential_file_permissions_are_too_broad(
    tmp_path: Path,
    caplog,
) -> None:
    credential_path = tmp_path / "credentials.json"
    secret = "must-not-appear-in-logs"
    credential_path.write_text(
        f'{{"{GEMINI_API_KEY.key}":"{secret}"}}',
        encoding="utf-8",
    )
    os.chmod(credential_path, 0o644)
    manager = CredentialManager(
        environment={},
        persistent_store=LocalFileSecretStore(credential_path),
    )

    with caplog.at_level(logging.WARNING, logger="CredentialManager"):
        value = manager.get(GEMINI_API_KEY)

    assert value is None
    assert "could not be read" in caplog.text
    assert "0600" in caplog.text
    assert secret not in caplog.text
    assert any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in logging.getLogger("CredentialManager").handlers
    )


def test_hanging_keyring_gives_up_instead_of_blocking(caplog) -> None:
    backend = HangingKeyringBackend()
    store = KeyringSecretStore(backend, timeout=0.2)

    try:
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="CredentialManager"):
            try:
                store.get(GEMINI_API_KEY.key)
            except KeyringUnavailableError:
                pass
            else:
                raise AssertionError("Expected a hanging keyring to be abandoned")

        assert time.monotonic() - started < 5
        assert "did not respond" in caplog.text
        assert "KERKOV_CREDENTIAL_STORE=file" in caplog.text
    finally:
        backend.released.set()


def test_hanging_keyring_is_skipped_after_the_first_stall() -> None:
    backend = HangingKeyringBackend()
    store = KeyringSecretStore(backend, timeout=0.2)

    try:
        try:
            store.get(GEMINI_API_KEY.key)
        except KeyringUnavailableError:
            pass

        assert store.is_available is False

        started = time.monotonic()
        try:
            store.get(GEMINI_API_KEY.key)
        except KeyringUnavailableError:
            pass
        else:
            raise AssertionError("Expected the retired keyring to stay unavailable")

        # The second call must not pay the deadline again.
        assert time.monotonic() - started < 0.2
        assert backend.calls == 1
    finally:
        backend.released.set()


def test_hanging_keyring_falls_through_to_the_local_file(tmp_path: Path) -> None:
    backend = HangingKeyringBackend()
    credential_path = tmp_path / "credentials.json"
    manager = CredentialManager(
        environment={},
        persistent_store=AutoSecretStore(
            KeyringSecretStore(backend, timeout=0.2),
            LocalFileSecretStore(credential_path),
        ),
    )

    try:
        # A stalled read reports the credential as missing, which is what makes
        # the interactive client prompt for it again.
        assert manager.get(GEMINI_API_KEY) is None

        result = manager.save(GEMINI_API_KEY, "re-entered-secret")

        assert result.persisted is True
        assert result.storage == "local file"
        assert manager.get(GEMINI_API_KEY) == "re-entered-secret"
    finally:
        backend.released.set()


def test_keyring_deadline_is_honored_below_the_notice_delay() -> None:
    backend = HangingKeyringBackend()
    store = KeyringSecretStore(backend, timeout=0.2)

    try:
        started = time.monotonic()
        try:
            store.get(GEMINI_API_KEY.key)
        except KeyringUnavailableError:
            pass
        else:
            raise AssertionError("Expected a hanging keyring to be abandoned")

        # The notice delay stages the wait; it must not extend a shorter one.
        elapsed = time.monotonic() - started
        assert elapsed < KEYRING_NOTICE_AFTER_SECONDS
    finally:
        backend.released.set()


def test_abandoned_keyring_write_cannot_outrank_a_newer_file_value(
    tmp_path: Path,
) -> None:
    backend = DeferredKeyringBackend()
    file_store = LocalFileSecretStore(tmp_path / "credentials.json")
    store = AutoSecretStore(KeyringSecretStore(backend, timeout=0.2), file_store)

    try:
        # The keyring stalls, so the first secret lands in the file instead.
        store.set(GEMINI_API_KEY.key, "old-secret")
        assert file_store.get(GEMINI_API_KEY.key) == "old-secret"

        # The user then rotates the credential, which the retired keyring
        # never sees.
        store.set(GEMINI_API_KEY.key, "new-secret")
        assert file_store.get(GEMINI_API_KEY.key) == "new-secret"
    finally:
        backend.released.set()

    # The abandoned write finally lands, leaving the keyring holding the value
    # the user already replaced.
    assert backend.applied.wait(30)
    assert backend.store[GEMINI_API_KEY.key] == "old-secret"

    # A later run with a healthy keyring must still resolve the rotation.
    restarted = AutoSecretStore(KeyringSecretStore(backend), file_store)
    assert restarted.get(GEMINI_API_KEY.key) == "new-secret"


def test_confirmed_keyring_write_restores_keyring_precedence(tmp_path: Path) -> None:
    backend = DeferredKeyringBackend()
    marker_key = GEMINI_API_KEY.key + KEYRING_INDETERMINATE_SUFFIX
    file_store = LocalFileSecretStore(tmp_path / "credentials.json")
    stalling = AutoSecretStore(KeyringSecretStore(backend, timeout=0.2), file_store)

    try:
        stalling.set(GEMINI_API_KEY.key, "old-secret")
        assert file_store.get(marker_key) is not None
    finally:
        backend.released.set()
    assert backend.applied.wait(30)

    healthy = AutoSecretStore(KeyringSecretStore(backend), file_store)
    healthy.set(GEMINI_API_KEY.key, "third-secret")

    # A write the keyring confirmed makes it authoritative again, so the
    # marker and the file copy are both retired.
    assert backend.store[GEMINI_API_KEY.key] == "third-secret"
    assert file_store.get(marker_key) is None
    assert file_store.get(GEMINI_API_KEY.key) is None
    assert healthy.get(GEMINI_API_KEY.key) == "third-secret"


def test_abandoned_keyring_delete_keeps_the_credential_forgotten(
    tmp_path: Path,
) -> None:
    backend = HangingKeyringBackend()
    file_store = LocalFileSecretStore(tmp_path / "credentials.json")
    store = AutoSecretStore(KeyringSecretStore(backend, timeout=0.2), file_store)

    try:
        try:
            store.delete(GEMINI_API_KEY.key)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected an abandoned delete to be reported")

        # The delete may never land, so the keyring must not resurrect the
        # credential on a later run.
        assert file_store.get(GEMINI_API_KEY.key + KEYRING_INDETERMINATE_SUFFIX)
    finally:
        backend.released.set()


def test_abandoned_keyring_does_not_promise_a_fallback_it_lacks(caplog) -> None:
    backend = HangingKeyringBackend()
    store = KeyringSecretStore(backend, timeout=0.2)

    try:
        with caplog.at_level(logging.WARNING, logger="CredentialManager"):
            try:
                store.get(GEMINI_API_KEY.key)
            except KeyringUnavailableError:
                pass

        # Used bare (KERKOV_CREDENTIAL_STORE=keyring) there is no file fallback.
        assert "credential file" not in caplog.text
        assert "KERKOV_CREDENTIAL_STORE=file" in caplog.text
    finally:
        backend.released.set()


def test_auto_store_names_the_local_file_it_falls_back_to(
    tmp_path: Path,
    caplog,
) -> None:
    backend = HangingKeyringBackend()
    file_store = LocalFileSecretStore(tmp_path / "credentials.json")
    store = AutoSecretStore(KeyringSecretStore(backend, timeout=0.2), file_store)

    try:
        with caplog.at_level(logging.WARNING, logger="CredentialManager"):
            assert store.get(GEMINI_API_KEY.key) is None

        assert "local file" in caplog.text
        assert str(file_store.path) in caplog.text
    finally:
        backend.released.set()


def test_file_mode_does_not_import_or_probe_the_keyring(tmp_path: Path) -> None:
    # A subprocess so the assertion is not defeated by another test having
    # already imported keyring into this interpreter.
    script = (
        "import sys\n"
        "from kerkov.core.credentials import _default_persistent_store\n"
        "store = _default_persistent_store()\n"
        "print(type(store).__name__)\n"
        "print('keyring' in sys.modules)\n"
    )
    environment = {
        **os.environ,
        "KERKOV_CREDENTIAL_STORE": "file",
        "KERKOV_CREDENTIAL_FILE": str(tmp_path / "credentials.json"),
    }

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["LocalFileSecretStore", "False"]


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
