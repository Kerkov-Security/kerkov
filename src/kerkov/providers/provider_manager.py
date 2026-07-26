from typing import Optional

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.credentials import CredentialManager, get_credential_manager
from kerkov.core.logging.logger_factory import get_logger
from kerkov.providers import create_initial_provider, create_provider_from_name
from kerkov.providers.base_provider import BaseLLMProvider

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

    def initialize_provider(self):
        logger.info(
            f"Initializing provider from config (provider={self._config.llm_provider})"
        )
        # Always refresh config so runtime auth state updates are picked up.
        self._config = get_config(reload=True)
        self._provider = create_initial_provider(self._config, self._credentials)
        logger.info(f"Provider initialized: {self._provider.__class__.__name__}")

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self._provider

    def set_provider(self, provider: BaseLLMProvider) -> None:
        """Install a provider supplied by an embedding runtime or test harness."""

        self._provider = provider

    def switch_provider(self, provider_name: str):
        self._provider = self.build_provider(provider_name)

    def build_provider(self, provider_name: str) -> BaseLLMProvider:
        """Construct a provider without changing the currently active provider."""

        # Reload configuration before switching to pick up recent runtime state edits.
        self._config = get_config(reload=True)
        provider = create_provider_from_name(
            self._config,
            provider_name,
            self._credentials,
        )
        if provider is None:
            logger.error(f"Unsupported provider: {provider_name}")
            raise ValueError(f"Unsupported provider: {provider_name}")
        return provider
