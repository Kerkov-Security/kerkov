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
    provider._available_models = ["main", "small", "large", "later"]
    return provider


@pytest.fixture
def manager(provider) -> ProviderManager:
    manager = ProviderManager(
        ConfigManager(
            AGENT_ROUTING_CHEAP_MODEL="small",
            AGENT_ROUTING_STRONG_MODEL="large",
        ),
        credentials=Mock(),
    )
    manager.set_provider(provider)
    return manager


def test_forks_hold_independent_model_and_catalog_state(provider) -> None:
    provider.token_manager = Mock()
    with patch("httpx.Client", side_effect=AssertionError("unexpected network call")):
        small = provider.fork("small")
        large = provider.fork("large")
        manual = provider.fork()

    provider.set_model("later")
    small._available_models.append("small-only")
    assert [small.model, large.model, manual.model, provider.model] == [
        "small",
        "large",
        "main",
        "later",
    ]
    assert "small-only" not in provider._available_models
    assert "small-only" not in large._available_models
    # Refresh coordination belongs to the account, not each independently routed run.
    assert small.token_manager is provider.token_manager
    assert large.token_manager is provider.token_manager


def test_fork_preserves_provider_local_model_validation(provider) -> None:
    with pytest.raises(CodexModelNotAvailableError):
        provider.fork("unavailable")

    assert provider.model == "main"


@pytest.mark.parametrize("model", ["", " ", "name with spaces"])
def test_fork_rejects_empty_or_malformed_model_ids(provider, model) -> None:
    with pytest.raises(ValueError, match="Model must"):
        provider.fork(model)


def test_enabling_routing_validates_pair_without_switching_main(
    manager, provider
) -> None:
    assert manager.agent_routing_enabled is False
    assert manager.get_agent_routing_policy() is None

    policy = manager.enable_agent_routing()

    assert manager.agent_routing_enabled is True
    assert (policy.cheap_model, policy.strong_model) == ("small", "large")
    assert manager.get_agent_routing_policy() is policy
    assert manager.provider is provider
    assert provider.model == "main"


def test_disabled_routing_leaves_running_forks_unchanged(manager, provider) -> None:
    policy = manager.enable_agent_routing()
    running = provider.fork(policy.cheap_model)

    provider.set_model("later")
    manager.disable_agent_routing()

    assert manager.get_agent_routing_policy() is None
    assert running.model == "small"
    assert provider.model == "later"


def test_replacing_provider_disables_pair_bound_to_previous_provider(manager) -> None:
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


def test_unavailable_model_does_not_enable_routing(manager, provider) -> None:
    provider._available_models.remove("small")
    with pytest.raises(CodexModelNotAvailableError):
        manager.enable_agent_routing()

    assert manager.agent_routing_enabled is False
    assert provider.model == "main"


def test_configured_startup_auto_mode_validates_before_first_delegate(provider) -> None:
    manager = ProviderManager(
        ConfigManager(AGENT_ROUTING_ENABLED=True), credentials=Mock()
    )
    manager.set_provider(provider)

    assert manager.agent_routing_enabled is True
    with pytest.raises(ValueError, match="AGENT_ROUTING_CHEAP_MODEL"):
        manager.get_agent_routing_policy()
    assert provider.model == "main"


def test_startup_initialization_preserves_auto_mode(provider) -> None:
    config = ConfigManager(
        AGENT_ROUTING_ENABLED=True,
        AGENT_ROUTING_CHEAP_MODEL="small",
        AGENT_ROUTING_STRONG_MODEL="large",
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
    assert manager.get_agent_routing_policy().cheap_model == "small"
