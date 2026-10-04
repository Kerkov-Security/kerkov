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
from kerkov.providers import workers
from kerkov.web.runtime import provider_settings, providers
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.provider_settings import normalize_ollama_url
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.provider_runtime_test import FakeProvider, request, session


@pytest.fixture
def setup_candidate(monkeypatch):
    candidate = FakeProvider("GeminiProvider", "missing-default")
    factory = Mock(return_value=candidate)
    monkeypatch.setattr(provider_settings, "create_provider_from_name", factory)
    return candidate, factory


@pytest.fixture
def durable_gemini(session):
    session.credentials.save(GEMINI_API_KEY, session.config.google_api_key)


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
    session, setup_candidate, settings_file, monkeypatch, tmp_path, durable_gemini
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
    session, setup_candidate, settings_file, monkeypatch, durable_gemini
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
    session, setup_candidate, monkeypatch, tmp_path, durable_gemini
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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["test", "configure", "default"])
@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_abandoned_setup_holds_shared_worker_until_real_check_exits(
    session, setup_candidate, monkeypatch, settings_file, operation, ending
):
    pool = workers.ProviderWorkerPool(max_workers=1)
    monkeypatch.setattr(workers, "_pool", pool)
    candidate, factory = setup_candidate
    started, release = threading.Event(), threading.Event()
    secret = "private-abandoned-setup-key"
    save = Mock()
    monkeypatch.setattr(session.credentials, "save", save)
    monkeypatch.setattr(providers, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.03)
    other = WebKerkovSession(WebEventBus(), config=session.config)

    def validate():
        started.set()
        release.wait(5)
        raise RuntimeError(secret)

    candidate.validate_connection = validate
    values = {"model": "new-model"} if operation == "default" else {"api_key": secret}
    task = asyncio.create_task(
        session.handle_provider_message(
            {
                "type": f"provider.{operation}",
                "request_id": "abandoned",
                "provider": "gemini",
                **values,
            }
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            event = session.bus.drain_nowait()[-1]
            assert event["type"] == "provider.selection_error"
            assert "timed out" in event["payload"]["detail"]
        assert not session.providers.switching
        # A different browser session shares the same real-call budget, across
        # both setup and picker paths. No retry may enter an executor queue.
        for blocked_operation, blocked_values in [
            ("settings", {}),
            ("test", {"provider": "gemini", "api_key": secret}),
            ("configure", {"provider": "gemini", "api_key": secret}),
            ("default", {"provider": "gemini", "model": "new-model"}),
            ("catalog", {}),
            ("models", {"provider": "gemini"}),
            ("switch", {"provider": "gemini", "model": "new-model"}),
        ]:
            event = await request(other, blocked_operation, **blocked_values)
            assert event["type"] == "provider.selection_error"
            assert event["payload"]["operation"] == blocked_operation
            assert "still running" in event["payload"]["detail"]
            assert secret not in str(event)
        assert factory.call_count == 1
        assert not settings_file.exists()
        save.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        pool.shutdown()
        other.close()
    # Neither a timed-out validation nor its late exception can write later.
    assert not settings_file.exists()
    save.assert_not_called()
    assert all(
        event["type"] not in {"provider.configured", "provider.default_saved"}
        for event in session.bus.drain_nowait()
    )


@pytest.mark.asyncio
async def test_timed_out_picker_worker_blocks_setup_until_it_really_exits(
    session, setup_candidate, monkeypatch
):
    pool = workers.ProviderWorkerPool(max_workers=1)
    monkeypatch.setattr(workers, "_pool", pool)
    monkeypatch.setattr(providers, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.03)
    candidate = FakeProvider("GeminiProvider", "new-model")
    started, release = threading.Event(), threading.Event()

    def models():
        started.set()
        release.wait(5)
        return ["new-model"]

    candidate.list_models = models
    monkeypatch.setattr(providers, "create_provider_from_name", lambda *args: candidate)
    task = asyncio.create_task(request(session, "models", provider="gemini"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        event = await task
        assert "timed out" in event["payload"]["detail"]
        for operation in ["settings", "test", "configure"]:
            values = (
                {}
                if operation == "settings"
                else {"provider": "gemini", "api_key": "private-key"}
            )
            event = await request(session, operation, **values)
            assert event["type"] == "provider.selection_error"
            assert "still running" in event["payload"]["detail"]
            assert "private-key" not in str(event)
        setup_candidate[1].assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", ["temporary", "failed", "config_only"])
async def test_default_rejects_key_unavailable_to_future_sessions(
    session, setup_candidate, settings_file, monkeypatch, persistence
):
    session.credentials = CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    if persistence != "config_only":
        if persistence == "failed":
            monkeypatch.setattr(
                session.credentials._persistent,
                "set",
                Mock(side_effect=OSError("private-store-error")),
            )
        configured = await request(
            session,
            "configure",
            provider="gemini",
            api_key="temporary-private-key",
            persist=persistence == "failed",
        )
        assert configured["payload"]["persisted"] is False
    settings_file.write_text('{"SEARCH_PROVIDER": "searxng"}\n')
    before = settings_file.read_bytes()
    old_config = session.config
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert event["payload"]["operation"] == "default"
    assert "Save setup" in event["payload"]["detail"]
    assert "future sessions" in event["payload"]["detail"]
    assert "private" not in str(event)
    assert session.config is old_config
    assert settings_file.read_bytes() == before
    assert session.credentials.new_session().get(GEMINI_API_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback,persist",
    [("environment", False), ("persistent", False), ("environment", True)],
)
async def test_default_rejects_validated_key_that_differs_from_future_fallback(
    session, setup_candidate, settings_file, fallback, persist
):
    store = MemorySecretStore()
    environment = (
        {"GOOGLE_API_KEY": "older-private-key"} if fallback == "environment" else {}
    )
    if fallback == "persistent":
        store.set(GEMINI_API_KEY.key, "older-private-key")
    session.credentials = CredentialManager(
        environment=environment, persistent_store=store
    )
    event = await request(
        session,
        "configure",
        provider="gemini",
        api_key="new-private-key",
        persist=persist,
    )
    assert event["type"] == "provider.configured"
    assert event["payload"]["persisted"] is persist
    # Even a confirmed persistent write cannot override a different env key.
    assert session.credentials.new_session().get(GEMINI_API_KEY) == "older-private-key"
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert "environment setting" in event["payload"]["detail"]
    assert "private-key" not in str(event)
    assert not settings_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["environment", "persistent"])
async def test_default_accepts_session_key_matching_future_fallback(
    session, setup_candidate, settings_file, fallback
):
    store = MemorySecretStore()
    environment = (
        {"GOOGLE_API_KEY": "same-private-key"} if fallback == "environment" else {}
    )
    if fallback == "persistent":
        store.set(GEMINI_API_KEY.key, "same-private-key")
    session.credentials = CredentialManager(
        environment=environment, persistent_store=store
    )
    await request(
        session,
        "configure",
        provider="gemini",
        api_key="same-private-key",
        persist=False,
    )
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.default_saved"
    assert json.loads(settings_file.read_text())["LLM_PROVIDER"] == "gemini"


@pytest.mark.asyncio
async def test_default_rechecks_persistent_key_instead_of_trusting_saved_source(
    session, setup_candidate, settings_file
):
    store = MemorySecretStore()
    session.credentials = CredentialManager(environment={}, persistent_store=store)
    configured = await request(
        session,
        "configure",
        provider="gemini",
        api_key="validated-private-key",
        persist=True,
    )
    assert configured["payload"]["persisted"] is True
    store.set(GEMINI_API_KEY.key, "replacement-private-key")
    event = await request(session, "default", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert "Save setup" in event["payload"]["detail"]
    assert not settings_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", ["temporary", "failed"])
async def test_default_rejects_ollama_url_not_available_to_future_sessions(
    session, setup_candidate, settings_file, monkeypatch, persistence
):
    if persistence == "failed":
        with monkeypatch.context() as patcher:
            patcher.setattr(
                provider_settings,
                "save_user_settings",
                Mock(side_effect=OSError("private-path")),
            )
            event = await request(
                session,
                "configure",
                provider="ollama",
                base_url="http://localhost:11435",
                persist=True,
            )
    else:
        event = await request(
            session,
            "configure",
            provider="ollama",
            base_url="http://localhost:11435",
            persist=False,
        )
    assert event["payload"]["persisted"] is False
    config = session.config
    event = await request(session, "default", provider="ollama", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert "Save setup" in event["payload"]["detail"]
    assert session.config is config
    assert not settings_file.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["http://localhost:11436", ""])
async def test_default_rejects_saved_ollama_url_overridden_by_environment(
    session, setup_candidate, settings_file, monkeypatch, override
):
    monkeypatch.setenv("OLLAMA_BASE", override)
    event = await request(
        session,
        "configure",
        provider="ollama",
        base_url="http://localhost:11435",
        persist=True,
    )
    assert event["payload"]["persisted"] is True
    before = settings_file.read_bytes()
    event = await request(session, "default", provider="ollama", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert "environment setting" in event["payload"]["detail"]
    assert settings_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["default", "saved", "environment"])
async def test_default_accepts_ollama_endpoint_available_to_future_sessions(
    session, setup_candidate, settings_file, monkeypatch, source
):
    if source == "environment":
        monkeypatch.setenv("OLLAMA_BASE", "http://localhost:11435")
    if source != "default":
        event = await request(
            session,
            "configure",
            provider="ollama",
            base_url="http://localhost:11435/",
            persist=source == "saved",
        )
        assert event["type"] == "provider.configured"
    event = await request(session, "default", provider="ollama", model="new-model")
    assert event["type"] == "provider.default_saved"
    assert json.loads(settings_file.read_text())["DEFAULT_OLLAMA_MODEL"] == "new-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "durable_url,configured_url,equivalent",
    [
        ("HTTP://LOCALHOST:11434/", "http://localhost:11434", True),
        ("http://localhost:80", "http://LOCALHOST", True),
        ("https://LOCALHOST", "https://localhost:443", True),
        ("http://[FE80::1%25Eth0]:11434", "http://[fe80::1%25Eth0]:11434", True),
        ("http://localhost:11434", "http://localhost:11435", False),
        ("http://localhost:443", "https://localhost", False),
        ("http://localhost", "http://localhost:0", False),
        ("http://[fe80::1%25Eth0]:11434", "http://[fe80::1%25eth0]:11434", False),
    ],
)
async def test_default_compares_canonical_ollama_origins(
    session,
    setup_candidate,
    settings_file,
    monkeypatch,
    durable_url,
    configured_url,
    equivalent,
):
    monkeypatch.setenv("OLLAMA_BASE", durable_url)
    configured = await request(
        session,
        "configure",
        provider="ollama",
        base_url=configured_url,
        persist=False,
    )
    assert configured["type"] == "provider.configured"
    assert configured["payload"]["base_url"] == configured_url
    assert session.config.ollama_base == configured_url

    event = await request(session, "default", provider="ollama", model="new-model")

    if equivalent:
        assert event["type"] == "provider.default_saved"
        assert (
            json.loads(settings_file.read_text())["DEFAULT_OLLAMA_MODEL"] == "new-model"
        )
    else:
        assert event["type"] == "provider.selection_error"
        assert event["payload"]["operation"] == "default"
        assert not settings_file.exists()
    # Origin comparison does not rewrite the user's chosen URL spelling.
    assert session.config.ollama_base == configured_url
