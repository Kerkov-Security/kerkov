"""Setup validates detached candidates before changing protected configuration."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import Mock

import pytest

from kerkov.core.config_manager import USER_SETTINGS_FILE_ENV_VAR, load_config
from kerkov.core.credentials import (
    GEMINI_API_KEY,
    CredentialManager,
    MemorySecretStore,
)
from kerkov.web.runtime import provider_settings, providers
from kerkov.web.runtime.provider_settings import normalize_ollama_url
from tests.web.provider_runtime_test import FakeProvider, request, session


@pytest.fixture
def setup_candidate(monkeypatch):
    candidate = FakeProvider("GeminiProvider", "missing-default")
    factory = Mock(return_value=candidate)
    monkeypatch.setattr(provider_settings, "create_provider_from_name", factory)
    return candidate, factory


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return path


@pytest.mark.asyncio
async def test_settings_reports_metadata_and_hides_unsafe_existing_url(session):
    session.config = session.config.model_copy(
        update={"ollama_base": "https://user:secret@example.com?token=private"}
    )
    event = await request(session, "settings")
    assert event["type"] == "provider.settings"
    rows = {item["id"]: item for item in event["payload"]["providers"]}
    assert rows["gemini"]["configured"] is True
    assert rows["gemini"]["auth_kind"] == "api_key"
    assert rows["codex"]["auth_kind"] == "oauth"
    assert rows["ollama"]["auth_kind"] == "local"
    assert "base_url" not in rows["ollama"]
    assert event["payload"]["warning"]
    assert "secret" not in str(event)
    assert "private" not in str(event)
    assert "example.com" not in str(event)


@pytest.mark.asyncio
async def test_testing_submitted_key_is_detached_and_never_saves(
    session, setup_candidate, settings_file
):
    candidate, factory = setup_candidate
    before = session.provider
    session.credentials.save(GEMINI_API_KEY, "existing-key")
    result = await request(
        session, "test", provider="gemini", api_key="new-private-key"
    )
    assert result["type"] == "provider.tested"
    assert result["payload"]["selected_model"] == "new-model"
    assert session.provider is before
    assert session.credentials.get(GEMINI_API_KEY) == "existing-key"
    assert factory.call_args.args[2] is not session.credentials
    assert factory.call_args.args[2].get(GEMINI_API_KEY) == "new-private-key"
    assert "new-private-key" not in str(result)
    assert not settings_file.exists()


@pytest.mark.asyncio
async def test_failed_configuration_does_not_replace_existing_key(
    session, setup_candidate
):
    candidate, _ = setup_candidate
    candidate.valid = False
    session.credentials.save(GEMINI_API_KEY, "old-key")
    event = await request(session, "configure", provider="gemini", api_key="bad-key")
    assert event["type"] == "provider.selection_error"
    assert event["payload"]["operation"] == "configure"
    assert session.credentials.get(GEMINI_API_KEY) == "old-key"


@pytest.mark.asyncio
async def test_validation_errors_do_not_emit_or_log_provider_error_bodies(
    session, setup_candidate, caplog
):
    candidate, _ = setup_candidate

    def fail():
        candidate.logger.error(
            "Error echoed new-secret-key at https://user:password@host"
        )
        raise RuntimeError("Error echoed new-secret-key at https://user:password@host")

    candidate.validate_connection = fail
    event = await request(
        session, "configure", provider="gemini", api_key="new-secret-key"
    )
    assert event["type"] == "provider.selection_error"
    assert "new-secret-key" not in str(event) + caplog.text
    assert "password" not in str(event) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True])
async def test_configure_saves_only_after_validation_without_switching(
    session, setup_candidate, persist
):
    before = session.provider
    store = MemorySecretStore()
    session.credentials = CredentialManager(environment={}, persistent_store=store)
    event = await request(
        session,
        "configure",
        provider="gemini",
        api_key="new-private-key",
        persist=persist,
    )
    assert event["type"] == "provider.configured"
    assert event["payload"]["persisted"] is persist
    assert session.credentials.get(GEMINI_API_KEY) == "new-private-key"
    assert store.get(GEMINI_API_KEY.key) == ("new-private-key" if persist else None)
    assert session.provider is before


@pytest.mark.asyncio
async def test_configure_blank_key_keeps_current_and_does_not_resave(
    session, setup_candidate, monkeypatch
):
    session.credentials.set_session(GEMINI_API_KEY, "existing")
    save = Mock(side_effect=AssertionError("must retain stored credential"))
    monkeypatch.setattr(session.credentials, "save", save)
    event = await request(
        session, "configure", provider="gemini", api_key="  ", persist=False
    )
    assert event["type"] == "provider.configured"
    assert "persisted" not in event["payload"]
    assert setup_candidate[1].call_args.args[2].get(GEMINI_API_KEY) == "existing"
    save.assert_not_called()


@pytest.mark.asyncio
async def test_persistent_key_failure_reports_session_only_fallback(
    session, setup_candidate, monkeypatch
):
    monkeypatch.setattr(
        session.credentials._persistent, "set", Mock(side_effect=OSError("private-key"))
    )
    event = await request(
        session, "configure", provider="gemini", api_key="new-private-key"
    )
    assert event["type"] == "provider.configured"
    assert event["payload"]["persisted"] is False
    assert "session only" in event["payload"]["warning"]
    assert session.credentials.get(GEMINI_API_KEY) == "new-private-key"
    assert "private-key" not in str(event)


@pytest.mark.asyncio
async def test_persisted_key_warns_about_environment_override(
    session, setup_candidate, monkeypatch
):
    monkeypatch.setenv("GOOGLE_API_KEY", "environment-key")
    event = await request(
        session, "configure", provider="gemini", api_key="new-private-key"
    )
    assert event["payload"]["persisted"] is True
    assert "environment" in event["payload"]["warning"]
    assert "environment-key" not in str(event)


@pytest.mark.asyncio
async def test_ollama_configuration_persists_normalized_url_and_preserves_active(
    session, setup_candidate, settings_file
):
    before = session.provider
    event = await request(
        session,
        "configure",
        provider="ollama",
        base_url="http://localhost:11435/",
        persist=True,
    )
    assert event["type"] == "provider.configured"
    assert event["payload"]["persisted"] is True
    assert event["payload"]["base_url"] == "http://localhost:11435"
    assert session.config.ollama_base == "http://localhost:11435"
    assert (
        json.loads(settings_file.read_text())["OLLAMA_BASE"] == "http://localhost:11435"
    )
    assert setup_candidate[1].call_args.args[0].ollama_base == "http://localhost:11435"
    assert session.provider is before


@pytest.mark.asyncio
async def test_ollama_test_and_failed_save_leave_disk_unchanged(
    session, setup_candidate, settings_file, monkeypatch
):
    old = session.config.ollama_base
    event = await request(
        session, "test", provider="ollama", base_url="http://localhost:11435"
    )
    assert event["type"] == "provider.tested"
    assert session.config.ollama_base == old
    assert not settings_file.exists()
    monkeypatch.setattr(
        provider_settings,
        "save_user_settings",
        Mock(side_effect=OSError("unsafe path")),
    )
    event = await request(
        session, "configure", provider="ollama", base_url="http://localhost:11435"
    )
    assert event["payload"]["persisted"] is False
    assert "session only" in event["payload"]["warning"]
    assert session.config.ollama_base == "http://localhost:11435"


@pytest.mark.asyncio
async def test_default_is_validated_and_saved_together_for_next_start(
    session, setup_candidate, settings_file, monkeypatch, tmp_path
):
    before = session.provider
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.default_saved"
    assert event["payload"]["default"] == {"provider": "gemini", "model": "new-model"}
    assert json.loads(settings_file.read_text()) == {
        "LLM_PROVIDER": "gemini",
        "DEFAULT_GEMINI_MODEL": "new-model",
    }
    monkeypatch.chdir(tmp_path)
    loaded = load_config(env_file=str(tmp_path / "missing.env"))
    assert loaded.llm_provider == "gemini"
    assert loaded.default_gemini_model == "new-model"
    assert session.provider is before


@pytest.mark.asyncio
async def test_default_failure_preserves_previous_preferences(
    session, setup_candidate, settings_file, monkeypatch
):
    old = session.config
    event = await request(session, "default", provider="gemini", model="unavailable")
    assert event["type"] == "provider.selection_error"
    assert not settings_file.exists()
    monkeypatch.setattr(
        provider_settings,
        "save_user_settings",
        Mock(side_effect=OSError("private path")),
    )
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert session.config is old
    assert "private path" not in str(event)


@pytest.mark.asyncio
async def test_default_environment_precedence_is_preserved_and_reported(
    session, setup_candidate, monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("DEFAULT_GEMINI_MODEL", "environment-model")
    event = await request(session, "default", provider="gemini", model="new-model")
    assert "environment" in event["payload"]["warning"]
    monkeypatch.chdir(tmp_path)
    loaded = load_config(env_file=str(tmp_path / "missing.env"))
    assert loaded.llm_provider == "anthropic"
    assert loaded.default_gemini_model == "environment-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["configure", "default"])
async def test_mutations_reject_busy_turn_or_active_agent(
    session, setup_candidate, operation, monkeypatch
):
    values = (
        {"model": "new-model"} if operation == "default" else {"api_key": "new-key"}
    )
    event = await request(session, operation, provider="gemini", pending=True, **values)
    assert event["type"] == "provider.selection_error"
    monkeypatch.setattr(session.agent_supervisor, "active", lambda: [object()])
    event = await request(session, operation, provider="gemini", **values)
    assert event["type"] == "provider.selection_error"
    setup_candidate[1].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "disconnect", "cancel"])
async def test_abandoned_validation_never_saves_later(
    session, setup_candidate, monkeypatch, ending
):
    candidate, _ = setup_candidate
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    save = Mock()
    monkeypatch.setattr(session.credentials, "save", save)

    def validate():
        started.set()
        release.wait(2)
        finished.set()
        return True

    candidate.validate_connection = validate
    if ending == "timeout":
        monkeypatch.setattr(providers, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.03)
    task = asyncio.create_task(
        session.handle_provider_message(
            {
                "type": "provider.configure",
                "request_id": "late",
                "provider": "gemini",
                "api_key": "new-key",
            }
        )
    )
    await asyncio.to_thread(started.wait, 1)
    if ending == "disconnect":
        session.close()
        release.set()
    elif ending == "cancel":
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        release.set()
    await asyncio.to_thread(finished.wait, 1)
    save.assert_not_called()
    assert not session.providers.switching


@pytest.mark.parametrize(
    "value",
    [
        "ftp://localhost",
        "http://user:secret@localhost",
        "http://localhost/path",
        "http://localhost?key=secret",
        "http://localhost#secret",
        "http://localhost:99999",
        "http://local host",
        "http://localhost\nsecret",
    ],
)
def test_invalid_endpoint_rejected_without_echoing(value):
    with pytest.raises(providers.ProviderSelectionError) as caught:
        normalize_ollama_url(value)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,values",
    [
        ("configure", {"provider": "gemini", "api_key": "secret\nkey"}),
        ("configure", {"provider": "gemini", "base_url": "http://localhost"}),
        ("configure", {"provider": "gemini", "persist": "yes"}),
        ("configure", {"provider": "gemini", "api_key": "a" * 4097}),
        ("configure", {"provider": "codex"}),
        ("test", {"provider": "ollama", "api_key": "secret"}),
        ("test", {"provider": "gemini", "persist": True}),
        ("default", {"provider": "gemini", "model": "new-model", "api_key": "secret"}),
    ],
)
async def test_invalid_inputs_never_construct_candidate(
    session, setup_candidate, operation, values
):
    event = await request(session, operation, **values)
    assert event["type"] == "provider.selection_error"
    setup_candidate[1].assert_not_called()


@pytest.mark.asyncio
async def test_session_key_can_be_persisted_without_retyping(session, setup_candidate):
    store = MemorySecretStore()
    session.credentials = CredentialManager(environment={}, persistent_store=store)
    event = await request(
        session,
        "configure",
        provider="gemini",
        api_key="new-private-key",
        persist=False,
    )
    assert event["payload"]["persisted"] is False
    assert store.get(GEMINI_API_KEY.key) is None
    event = await request(
        session, "configure", provider="gemini", api_key="", persist=True
    )
    assert event["payload"]["persisted"] is True
    assert store.get(GEMINI_API_KEY.key) == "new-private-key"
    assert "new-private-key" not in str(event)


@pytest.mark.asyncio
async def test_picker_cannot_reflect_credential_in_model_name(session, monkeypatch):
    candidate = FakeProvider("GeminiProvider")
    candidate.models = ["new-model", "private-test-key"]
    monkeypatch.setattr(providers, "create_provider_from_name", lambda *args: candidate)
    event = await request(session, "models", provider="gemini")
    assert event["type"] == "provider.selection_error"
    assert "private-test-key" not in str(event)


@pytest.mark.asyncio
async def test_saved_codex_login_can_be_tested_and_selected_as_default(
    session, monkeypatch, settings_file
):
    candidate = FakeProvider("CodexProvider", "new-model")
    monkeypatch.setattr(
        session.providers, "_build_candidate", lambda provider: candidate
    )
    event = await request(session, "test", provider="codex")
    assert event["type"] == "provider.tested"
    assert event["payload"]["selected_model"] == "new-model"
    event = await request(session, "default", provider="codex", model="new-model")
    assert event["type"] == "provider.default_saved"
    assert json.loads(settings_file.read_text()) == {
        "LLM_PROVIDER": "codex",
        "DEFAULT_CODEX_MODEL": "new-model",
    }
