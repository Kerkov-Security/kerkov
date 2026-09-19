from typing import Any, Mapping, Optional

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.credentials import CredentialManager, get_credential_manager
from kerkov.core.logging.logger_factory import get_logger
from kerkov.providers import create_initial_provider, create_provider_from_name
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.routing import RoutingPolicy

logger = get_logger("ProviderManager")


class ProviderManager:

    def __init__(
        self,
        config: ConfigManager,
        credentials: Optional[CredentialManager] = None,
    ):
        self._config = config
        self._credentials = credentials or get_credential_manager()
        self._provider: Optional[BaseLLMProvider] = None
        self.agent_routing_enabled = config.agent_routing_enabled
        self._agent_routing_policy: Optional[RoutingPolicy] = None

    def initialize_provider(self):
        logger.info(
            f"Initializing provider from config (provider={self._config.llm_provider})"
        )
        # Always refresh config so runtime auth state updates are picked up.
        self._config = get_config(reload=True)
        self.set_provider(create_initial_provider(self._config, self._credentials))
        logger.info(f"Provider initialized: {self._provider.__class__.__name__}")

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self._provider

    def set_provider(self, provider: BaseLLMProvider) -> None:
        """Install a provider supplied by an embedding runtime or test harness."""
        if self._provider is not None and self._provider is not provider:
            # A configured pair belongs to the provider selected when enabled.
            self.disable_agent_routing()
        self._provider = provider

    def clear_provider(self) -> None:
        """Return to provider-free setup mode."""

        self._provider = None
        self.disable_agent_routing()

    def switch_provider(self, provider_name: str):
        self.set_provider(self.build_provider(provider_name))

    def enable_agent_routing(self) -> RoutingPolicy:
        """Validate the configured pair locally and enable future delegate routing.

        Remote model availability is handled by the provider at execution time.
        No request is made and the main conversation keeps its current model.
        """
        if self._provider is None:
            raise ValueError("Select a provider before enabling agent model routing.")
        policy = RoutingPolicy(
            cheap_model=self._config.agent_routing_cheap_model,
            strong_model=self._config.agent_routing_strong_model,
        )
        self._provider.fork(policy.cheap_model)
        self._provider.fork(policy.strong_model)
        self._agent_routing_policy = policy
        self.agent_routing_enabled = True
        return policy

    def disable_agent_routing(self) -> None:
        """Restore manual selection for future agents; running forks are unchanged."""
        self.agent_routing_enabled = False
        self._agent_routing_policy = None

    def get_agent_routing_policy(self) -> Optional[RoutingPolicy]:
        """Return the validated policy, including lazy validation at startup."""
        if not self.agent_routing_enabled:
            return None
        if self._agent_routing_policy is None:
            return self.enable_agent_routing()
        return self._agent_routing_policy

    def build_provider(
        self,
        provider_name: str,
        *,
        config_overrides: Optional[Mapping[str, Any]] = None,
    ) -> BaseLLMProvider:
        """Construct a provider without changing the currently active provider."""

        # Reload configuration before switching to pick up recent runtime state edits.
        self._config = get_config(reload=True)
        if config_overrides:
            self._config = self._config.model_copy(update=dict(config_overrides))
        provider = create_provider_from_name(
            self._config,
            provider_name,
            self._credentials,
        )
        if provider is None:
            logger.error(f"Unsupported provider: {provider_name}")
            raise ValueError(f"Unsupported provider: {provider_name}")
        return provider
