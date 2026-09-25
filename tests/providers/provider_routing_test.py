"""Provider forks isolate model selection without duplicating auth resources."""

from unittest.mock import Mock, patch

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.provider import CodexModelNotAvailableError, CodexProvider
from kerkov.providers.provider_manager import ProviderManager


@pytest.fixture
def provider() -> CodexProvider:
    provider = CodexProvider(
        model="main",
        base_url="https://example.invalid",
        access_token="test-token",
    )
    provider._available_models = ["main", "small", "medium", "large", "later"]
    return provider


@pytest.fixture
def manager(provider) -> ProviderManager:
    manager = ProviderManager(
        ConfigManager(
            AGENT_ROUTING_SMALL_MODEL="small",
            AGENT_ROUTING_MEDIUM_MODEL="medium",
            AGENT_ROUTING_LARGE_MODEL="large",
        ),
        credentials=Mock(),
    )
    manager.set_provider(provider)
    return manager


def test_forks_hold_independent_model_and_catalog_state(provider) -> None:
    provider.token_manager = Mock()
    with patch("httpx.Client", side_effect=AssertionError("unexpected network call")):
        small = provider.fork("small")
        medium = provider.fork("medium")
        large = provider.fork("large")
        manual = provider.fork()

    provider.set_model("later")
    small._available_models.append("small-only")
    assert [small.model, medium.model, large.model, manual.model, provider.model] == [
        "small",
        "medium",
        "large",
        "main",
        "later",
    ]
    assert "small-only" not in provider._available_models
    assert "small-only" not in large._available_models
    assert "small-only" not in medium._available_models
    # Refresh coordination belongs to the account, not each independently routed run.
    assert small.token_manager is provider.token_manager
    assert large.token_manager is provider.token_manager
    assert medium.token_manager is provider.token_manager


def test_fork_preserves_provider_local_model_validation(provider) -> None:
    with pytest.raises(CodexModelNotAvailableError):
        provider.fork("unavailable")

    assert provider.model == "main"


@pytest.mark.parametrize("model", ["", " ", "name with spaces"])
def test_fork_rejects_empty_or_malformed_model_ids(provider, model) -> None:
    with pytest.raises(ValueError, match="Model must"):
        provider.fork(model)


def test_enabling_routing_validates_tiers_without_switching_main(
    manager, provider
) -> None:
    assert manager.agent_routing_enabled is False
    assert manager.get_agent_routing_policy() is None

    policy = manager.enable_agent_routing()

    assert manager.agent_routing_enabled is True
    assert (policy.small_model, policy.medium_model, policy.large_model) == (
        "small",
        "medium",
        "large",
    )
    assert manager.get_agent_routing_policy() is policy
    assert manager.provider is provider
    assert provider.model == "main"


@pytest.mark.parametrize("tier", ["small", "medium", "large"])
def test_disabled_routing_leaves_running_forks_unchanged(
    manager, provider, tier
) -> None:
    policy = manager.enable_agent_routing()
    running = provider.fork(getattr(policy, f"{tier}_model"))

    provider.set_model("later")
    manager.disable_agent_routing()

    assert manager.get_agent_routing_policy() is None
    assert running.model == tier
    assert provider.model == "later"


def test_replacing_provider_disables_tiers_bound_to_previous_provider(manager) -> None:
    manager.enable_agent_routing()
    replacement = CodexProvider("new", "https://example.invalid", "test-token")

    manager.set_provider(replacement)

    assert manager.provider is replacement
    assert manager.agent_routing_enabled is False
    assert manager.get_agent_routing_policy() is None


def test_switch_provider_disables_routing(manager, provider) -> None:
    manager.enable_agent_routing()
    replacement = provider.fork()
    with patch.object(manager, "build_provider", return_value=replacement):
        manager.switch_provider("codex")

    assert manager.provider is replacement
    assert manager.get_agent_routing_policy() is None


def test_clearing_provider_disables_routing(manager) -> None:
    manager.enable_agent_routing()
    manager.clear_provider()

    assert manager.provider is None
    assert manager.agent_routing_enabled is False
    with pytest.raises(ValueError, match="Select a provider"):
        manager.enable_agent_routing()


@pytest.mark.parametrize("unavailable", ["small", "medium", "large"])
def test_unavailable_model_does_not_enable_routing(
    manager, provider, unavailable
) -> None:
    provider._available_models.remove(unavailable)
    with pytest.raises(CodexModelNotAvailableError):
        manager.enable_agent_routing()

    assert manager.agent_routing_enabled is False
    assert provider.model == "main"


def test_missing_medium_model_does_not_enable_routing(provider) -> None:
    manager = ProviderManager(
        ConfigManager(
            AGENT_ROUTING_SMALL_MODEL="small",
            AGENT_ROUTING_LARGE_MODEL="large",
        ),
        credentials=Mock(),
    )
    manager.set_provider(provider)

    with pytest.raises(ValueError, match="AGENT_ROUTING_MEDIUM_MODEL"):
        manager.enable_agent_routing()

    assert manager.agent_routing_enabled is False
    assert provider.model == "main"


def test_lazy_invalid_auto_mode_disables_once_and_restores_manual(
    provider, caplog
) -> None:
    manager = ProviderManager(
        ConfigManager(AGENT_ROUTING_ENABLED=True), credentials=Mock()
    )
    manager.set_provider(provider)

    assert manager.agent_routing_enabled is True
    assert manager.get_agent_routing_policy() is None
    assert manager.get_agent_routing_policy() is None
    assert manager.agent_routing_enabled is False
    warnings = [
        record.message for record in caplog.records if record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "AGENT_ROUTING_SMALL_MODEL" in warnings[0]
    assert "selected conversation model" in warnings[0]
    assert provider.model == "main"


def test_startup_initialization_preserves_auto_mode(provider) -> None:
    config = ConfigManager(
        AGENT_ROUTING_ENABLED=True,
        AGENT_ROUTING_SMALL_MODEL="small",
        AGENT_ROUTING_MEDIUM_MODEL="medium",
        AGENT_ROUTING_LARGE_MODEL="large",
    )
    manager = ProviderManager(config, credentials=Mock())
    with (
        patch("kerkov.providers.provider_manager.get_config", return_value=config),
        patch(
            "kerkov.providers.provider_manager.create_initial_provider",
            return_value=provider,
        ),
    ):
        manager.initialize_provider()

    assert manager.agent_routing_enabled is True
    assert manager.get_agent_routing_policy().small_model == "small"


@pytest.mark.parametrize(
    "models,reason",
    [
        (("", "medium", "large"), "AGENT_ROUTING_SMALL_MODEL"),
        (("small", "", "large"), "AGENT_ROUTING_MEDIUM_MODEL"),
        (("small", "small", "large"), "three different model IDs"),
        (("unavailable", "medium", "large"), "unavailable"),
    ],
)
def test_startup_invalid_auto_mode_warns_and_uses_manual(
    provider, caplog, models, reason
):
    config = ConfigManager(
        AGENT_ROUTING_ENABLED=True,
        AGENT_ROUTING_SMALL_MODEL=models[0],
        AGENT_ROUTING_MEDIUM_MODEL=models[1],
        AGENT_ROUTING_LARGE_MODEL=models[2],
    )
    manager = ProviderManager(config, credentials=Mock())
    with (
        patch("kerkov.providers.provider_manager.get_config", return_value=config),
        patch(
            "kerkov.providers.provider_manager.create_initial_provider",
            return_value=provider,
        ),
    ):
        manager.initialize_provider()

    assert manager.agent_routing_enabled is False
    assert manager.get_agent_routing_policy() is None
    assert manager.provider is provider
    assert provider.model == "main"
    assert reason in caplog.text
