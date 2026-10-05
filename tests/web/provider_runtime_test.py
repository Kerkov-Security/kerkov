"""Configured-provider selection, atomic activation, and runtime preservation."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.models.basic_models import LLMResponse, Message
from kerkov.providers import workers
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.codex.auth import CodexAuthConfig, CodexTokenManager
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.workers import ProviderWorkerPool
from kerkov.web.runtime import providers as provider_runtime
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession


class FakeProvider(BaseLLMProvider):
    def __init__(self, name: str = "AnthropicProvider", model: str = "old-model"):
        super().__init__(api_key="private-test-key", model=model)
        self.name = name
        self.models = ["new-model", "old-model"]
        self.valid = True
        self.context_limit: Optional[int] = 32_000
        self.requests: list[list[Message]] = []

    def get_model_info(self) -> dict[str, str]:
        return {"provider": self.name, "model": self.model}

    async def complete(self, messages, **kwargs) -> LLMResponse:
        self.requests.append(list(messages))
        return LLMResponse(content="continued", model=self.model)

    def list_models(self) -> list[str]:
        return self.models

    def validate_connection(self) -> bool:
        return self.valid

    async def get_max_context_tokens(self) -> Optional[int]:
        return self.context_limit

    async def get_max_output_tokens(self) -> Optional[int]:
        return 1024


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    credentials = CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    monkeypatch.setattr(
        "kerkov.web.runtime.session.get_credential_manager", lambda: credentials
    )
    web = WebKerkovSession(
        WebEventBus(),
        config=ConfigManager(
            ANTHROPIC_API_KEY="private-test-key",
            GOOGLE_API_KEY="private-test-google-key",
            KB_ENABLED=True,
            EXECUTION_ENABLED=False,
            WORKSPACE_DIR=str(tmp_path / "workspaces"),
            USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
            HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
            codex_state_path=str(tmp_path / "codex-auth.json"),
        ),
    )
    web.provider_manager.set_provider(FakeProvider())
    yield web
    web.close()


@pytest.fixture
def candidate(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    result = FakeProvider("GeminiProvider", "missing-default")
    monkeypatch.setattr(
        provider_runtime, "create_provider_from_name", lambda *args: result
    )
    return result


async def request(
    web: WebKerkovSession, operation: str, *, pending: bool = False, **values: Any
) -> dict:
    web.bus.drain_nowait()
    await web.handle_provider_message(
        {"type": f"provider.{operation}", "request_id": "request-1", **values},
        message_pending=pending,
    )
    events = web.bus.drain_nowait()
    assert len(events) == 1, events
    return events[0]


@pytest.mark.asyncio
async def test_catalog_reports_only_configured_flags_and_canonical_active(
    session: WebKerkovSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = Mock(side_effect=AssertionError("catalog must not instantiate providers"))
    monkeypatch.setattr(provider_runtime, "create_provider_from_name", build)
    event = await request(session, "catalog")
    assert event["type"] == "provider.catalog"
    payload = event["payload"]
    assert payload["active"] == {"provider": "anthropic", "model": "old-model"}
    configured = {row["id"]: row["configured"] for row in payload["providers"]}
    assert configured == {
        "anthropic": True,
        "gemini": True,
        "nvidia": False,
        "openrouter": False,
        "deepseek": False,
        "ollama": True,
        "codex": False,
    }
    assert all(
        set(row) == {"id", "label", "configured"} for row in payload["providers"]
    )
    assert "private-test" not in str(event)
    build.assert_not_called()


@pytest.mark.asyncio
async def test_models_use_detached_candidate_and_valid_selected_fallback(
    session: WebKerkovSession,
    candidate: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = session.provider
    factory = Mock(return_value=candidate)
    monkeypatch.setattr(provider_runtime, "create_provider_from_name", factory)
    event = await request(session, "models", provider="gemini")
    assert event["payload"]["selected_model"] == "new-model"
    assert session.provider is old
    assert old.model == "old-model"
    config, name, credentials = factory.call_args.args
    assert config is not session.config
    assert name == "gemini"
    assert credentials is session.credentials
    event = await request(session, "models", provider="anthropic")
    assert event["payload"]["selected_model"] == "old-model"


@pytest.mark.asyncio
async def test_switch_preserves_engagement_inventory_agents_history_and_accounting(
    session: WebKerkovSession, candidate: FakeProvider
) -> None:
    session._apply_system_prompt_if_needed()
    session._append_message(Message(role="user", content="prior question"))
    session._append_message(Message(role="assistant", content="prior answer"))
    ledger = session.findings_ledger
    assert ledger is not None
    await ledger.add(
        LedgerSection.ASSETS,
        {"kind": "domain", "value": "example.com"},
        engagement_id=session.engagement_store.engagement_id,
    )
    agent = AgentSession(
        id="a1",
        agent_run_id="agent-run-1",
        task="finished task",
        profile="general",
        state=AgentState.DONE,
    )
    session.agent_supervisor._sessions[agent.id] = agent
    session.token_tracker.record_usage(100, 20)
    session.token_tracker.record_side_spend(5, 3)
    session.provider.context_limit = 100_000
    await session._ensure_compactor()
    session.compaction_warning_shown = True
    history = session.conversation_history
    prior_messages = list(history)
    engagement = session.engagement_store.engagement_id
    workspace = session.workspace_path
    inventory_context = session.inventory.context_id
    compactor = session.compactor
    supervisor = session.agent_supervisor

    event = await request(session, "switch", provider="gemini", model="new-model")

    assert event["type"] == "provider.changed"
    assert event["payload"] == {
        "request_id": "request-1",
        "active": {"provider": "gemini", "model": "new-model"},
        "provider": {"provider": "GeminiProvider", "model": "new-model"},
        "provider_ready": True,
    }
    assert session.provider is candidate
    assert session.conversation_history is history
    assert session.conversation_history == prior_messages
    assert session.engagement_store.engagement_id == engagement
    assert session.workspace_path == workspace
    assert session.findings_ledger is ledger
    assert session.inventory.context_id == inventory_context
    assert session.agent_supervisor is supervisor
    assert session.agent_supervisor.list() == [agent]
    assert session.compactor is compactor
    assert compactor.max_tokens == 32_000
    assert compactor.ledger_active
    assert not session.compaction_warning_shown
    assert session.token_tracker.total_tokens == 120
    assert session.token_tracker.cumulative_tokens == 128
    rows = await session.knowledge_base.list_ledger_entries(
        section=LedgerSection.ASSETS
    )
    assert rows[0].entry["value"] == "example.com"
    assert not session.providers.switching
    assert not session._lock.locked()

    await session.handle_user_message("continue")
    assert candidate.requests
    assert "prior answer" in [message.content for message in candidate.requests[0]]
    assert session.engagement_store.engagement_id == engagement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["catalog", "unknown", "validation", "fallback", "context"]
)
async def test_failed_switch_preserves_active_provider_and_compactor(
    session: WebKerkovSession,
    candidate: FakeProvider,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = session.provider
    await session._ensure_compactor()
    compactor = session.compactor
    if failure == "catalog":
        monkeypatch.setattr(
            candidate,
            "list_models",
            Mock(side_effect=ValueError("private-test-secret")),
        )
    elif failure == "unknown":
        candidate.models = ["other-model"]
    elif failure == "validation":
        candidate.valid = False
    elif failure == "fallback":

        def validate() -> bool:
            candidate.model = "fallback-model"
            return True

        monkeypatch.setattr(candidate, "validate_connection", validate)
    elif failure == "context":
        monkeypatch.setattr(
            candidate,
            "get_max_context_tokens",
            AsyncMock(side_effect=ValueError("private-test-secret")),
        )
    event = await request(session, "switch", provider="gemini", model="new-model")
    assert event["type"] == "provider.selection_error"
    assert event["payload"]["operation"] == "switch"
    assert "private-test" not in str(event)
    assert session.provider is old
    assert old.model == "old-model"
    assert session.compactor is compactor
    assert compactor.max_tokens == 32_000
    assert not session.providers.switching
    assert not session._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["pending", "lock", "agents", "switching"])
async def test_switch_rejected_while_work_is_pending(
    session: WebKerkovSession, candidate: FakeProvider, busy: str
) -> None:
    old = session.provider
    if busy == "lock":
        await session._lock.acquire()
    elif busy == "agents":
        session.agent_supervisor._sessions["a1"] = AgentSession(
            id="a1",
            agent_run_id="agent-run-1",
            profile="general",
            task="running",
            state=AgentState.RUNNING,
        )
    elif busy == "switching":
        session.providers.switching = True
    try:
        event = await request(
            session,
            "switch",
            provider="gemini",
            model="new-model",
            pending=busy == "pending",
        )
    finally:
        if busy == "lock":
            session._lock.release()
        session.providers.switching = False
    assert event["type"] == "provider.selection_error"
    assert session.provider is old
    assert candidate.model == "missing-default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"request_id": ""},
        {"request_id": "a" * 129},
        {"request_id": "bad\nrequest"},
        {"request_id": []},
        {"provider": "claude"},
        {"provider": "unsupported"},
        {"provider": []},
        {"model": ""},
        {"model": "a" * 257},
        {"model": "bad\nmodel"},
        {"model": {}},
    ],
)
async def test_invalid_selection_never_builds_a_provider(
    session: WebKerkovSession, values: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Mock(side_effect=AssertionError("unexpected factory call"))
    monkeypatch.setattr(provider_runtime, "create_provider_from_name", factory)
    event = await request(
        session, "switch", **{"provider": "gemini", "model": "new-model", **values}
    )
    assert event["type"] == "provider.selection_error"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_unconfigured_provider_rejected_without_factory_call(
    session: WebKerkovSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Mock(side_effect=AssertionError("unexpected factory call"))
    monkeypatch.setattr(provider_runtime, "create_provider_from_name", factory)
    event = await request(session, "models", provider="deepseek")
    assert event["type"] == "provider.selection_error"
    assert "not configured" in event["payload"]["detail"]
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "close", "timeout"])
async def test_abandoned_worker_cannot_commit_late(
    session: WebKerkovSession,
    candidate: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    old = session.provider

    def validate() -> bool:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(timeout=3)
            return True
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(candidate, "validate_connection", validate)
    if finish == "timeout":
        monkeypatch.setattr(provider_runtime, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.02)
    task = asyncio.create_task(
        session.handle_provider_message(
            {
                "type": "provider.switch",
                "request_id": "request-1",
                "provider": "gemini",
                "model": "new-model",
            }
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert session.providers.switching
        assert session._lock.locked()
        if finish == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif finish == "close":
            session.close()
        else:
            await asyncio.wait_for(task, timeout=1)
            event = session.bus.drain_nowait()[-1]
            assert event["type"] == "provider.selection_error"
            assert "timed out" in event["payload"]["detail"]
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)
        if not task.done():
            await asyncio.wait_for(task, timeout=1)
    assert session.provider is old
    assert not session.providers.switching
    assert not session._lock.locked()
    assert session.bus.drain_nowait() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["catalog", "models", "switch"])
async def test_timed_out_provider_worker_bounds_retries_across_sessions(
    session: WebKerkovSession,
    candidate: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    pool = ProviderWorkerPool(max_workers=1)
    monkeypatch.setattr(workers, "_pool", pool)
    monkeypatch.setattr(provider_runtime, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.1)
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    calls = []
    old = session.provider
    other = WebKerkovSession(WebEventBus(), config=session.config)
    other.provider_manager.set_provider(FakeProvider())
    other_old = other.provider

    def hold(*args):
        calls.append(args)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(3)
            return {
                "catalog": [],
                "models": (["new-model"], "new-model"),
                "switch": candidate,
            }[operation]
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(
        session.providers,
        {
            "catalog": "_catalog",
            "models": "_load_models",
            "switch": "_validated_candidate",
        }[operation],
        hold,
    )
    task = asyncio.create_task(
        request(session, operation, provider="gemini", model="new-model")
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        event = await asyncio.wait_for(task, 1)
        assert event["type"] == "provider.selection_error"
        assert "timed out" in event["payload"]["detail"]
        # A new request task, including one in another browser session, cannot
        # free the expired request's real slot or queue more provider work.
        for web in (session, other):
            for retry in ("catalog", "models", "switch"):
                rejected = await asyncio.wait_for(
                    request(web, retry, provider="gemini", model="new-model"), 0.5
                )
                assert rejected["type"] == "provider.selection_error"
                assert rejected["payload"]["operation"] == retry
                assert (
                    rejected["payload"]["detail"]
                    == "Provider checks are still running. Wait and retry."
                )
                assert not web.providers.switching
                assert not web._lock.locked()
        assert len(calls) == 1
        assert (
            await asyncio.wait_for(asyncio.to_thread(lambda: "responsive"), 0.5)
            == "responsive"
        )
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)
        pool.shutdown()
        other.close()
    assert session.provider is old
    assert other.provider is other_old
    assert session.bus.drain_nowait() == []
    assert other.bus.drain_nowait() == []


@pytest.mark.asyncio
async def test_empty_catalog_returns_error_and_context_falls_back_when_unknown(
    session: WebKerkovSession, candidate: FakeProvider
) -> None:
    candidate.models = []
    event = await request(session, "models", provider="gemini")
    assert event["type"] == "provider.selection_error"
    candidate.models = ["new-model"]
    candidate.context_limit = None
    event = await request(session, "switch", provider="gemini", model="new-model")
    assert event["type"] == "provider.changed"
    assert session.compactor.max_tokens == session.config.max_context_tokens


@pytest.mark.asyncio
async def test_selection_can_recover_provider_free_session_without_starting_engagement(
    session: WebKerkovSession, candidate: FakeProvider
) -> None:
    session.provider_manager.clear_provider()
    event = await request(session, "catalog")
    assert event["payload"]["active"] is None
    event = await request(session, "switch", provider="gemini", model="new-model")
    assert event["type"] == "provider.changed"
    assert session.engagement_store.engagement_id is None
    assert session.conversation_history == []


@pytest.mark.asyncio
async def test_codex_picker_refresh_updates_active_auth_and_survives_switching_away(
    session: WebKerkovSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.config = session.config.model_copy(
        update={
            "codex_access_token": "expired-token",
            "codex_oauth_refresh_token": "old-refresh",
            "codex_account_id": "test-account",
            "codex_token_expires_at": "1",
        }
    )
    active = CodexProvider(
        model="old-model",
        base_url="https://example.invalid",
        access_token="expired-token",
        account_id="test-account",
        config=session.config,
    )
    session.provider_manager.set_provider(active)
    manager = active.token_manager
    assert manager is not None

    def refresh() -> None:
        manager.access_token = "fresh-token"
        manager.refresh_token = "rotated-refresh"
        manager.expires_at = int(time.time()) + 3600

    def models(provider: CodexProvider) -> list[str]:
        provider._ensure_auth()
        provider._available_models = ["new-model", "old-model"]
        return provider._available_models

    refresh_mock = Mock(side_effect=refresh)
    monkeypatch.setattr(manager, "_refresh_access_token", refresh_mock)
    monkeypatch.setattr(CodexProvider, "list_models", models)
    state_loader = Mock(side_effect=AssertionError("must use the active manager"))
    monkeypatch.setattr(provider_runtime, "load_codex_state", state_loader)

    event = await request(session, "models", provider="codex")
    assert event["type"] == "provider.models"
    assert session.provider is active
    assert active.model == "old-model"
    active._ensure_auth()
    assert active.access_token == "fresh-token"
    assert manager.refresh_token == "rotated-refresh"
    refresh_mock.assert_called_once()

    session.provider_manager.set_provider(FakeProvider())
    event = await request(session, "switch", provider="codex", model="new-model")
    assert event["type"] == "provider.changed"
    assert session.provider is not active
    assert session.provider.token_manager is manager
    assert session.provider.access_token == "fresh-token"
    assert session.config.codex_access_token == "expired-token"
    refresh_mock.assert_called_once()
    state_loader.assert_not_called()


@pytest.mark.asyncio
async def test_first_codex_candidate_reads_persisted_auth_once_and_retains_manager(
    session: WebKerkovSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_loader = Mock(
        return_value={
            "CODEX_ACCESS_TOKEN": "stored-token",
            "CODEX_OAUTH_REFRESH_TOKEN": "stored-refresh",
            "CODEX_ACCOUNT_ID": "test-account",
            "CODEX_TOKEN_EXPIRES_AT": str(int(time.time()) + 3600),
        }
    )
    monkeypatch.setattr(provider_runtime, "load_codex_state", state_loader)

    def models(provider: CodexProvider) -> list[str]:
        provider._ensure_auth()
        provider._available_models = ["new-model"]
        return provider._available_models

    monkeypatch.setattr(CodexProvider, "list_models", models)
    event = await request(session, "models", provider="codex")
    assert event["type"] == "provider.models"
    manager = session.providers._codex_token_manager
    assert manager is not None
    event = await request(session, "switch", provider="codex", model="new-model")
    assert event["type"] == "provider.changed"
    assert session.provider.token_manager is manager
    state_loader.assert_called_once_with(Path(session.config.codex_state_path))
    assert session.config.codex_access_token is None
    event = await request(session, "catalog")
    codex = next(row for row in event["payload"]["providers"] if row["id"] == "codex")
    assert codex["configured"] is True


@pytest.mark.asyncio
async def test_shared_codex_manager_refreshes_expired_token_once_for_concurrent_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = CodexTokenManager(
        CodexAuthConfig(
            state_path=tmp_path / "auth.json",
            token_url="https://example.invalid/token",
            client_id="test-client",
            client_secret=None,
            timeout_seconds=1,
        ),
        access_token="expired-token",
        refresh_token="old-refresh",
        account_id="test-account",
        expires_at="1",
    )
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    second_started = asyncio.Event()
    release = threading.Event()
    refreshes: list[str] = []

    def refresh() -> None:
        refreshes.append(manager.refresh_token)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=3)
        manager.access_token = "fresh-token"
        manager.refresh_token = "rotated-refresh"
        manager.expires_at = int(time.time()) + 3600

    def second_user() -> str:
        loop.call_soon_threadsafe(second_started.set)
        return manager.ensure_valid_token()

    monkeypatch.setattr(manager, "_refresh_access_token", refresh)
    first = asyncio.create_task(asyncio.to_thread(manager.ensure_valid_token))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(asyncio.to_thread(second_user))
        await asyncio.wait_for(second_started.wait(), timeout=1)
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert results == ["fresh-token", "fresh-token"]
    assert refreshes == ["old-refresh"]
