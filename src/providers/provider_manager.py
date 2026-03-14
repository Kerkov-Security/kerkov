from typing import Optional

from src.core.config_manager import ConfigManager, get_config
from src.providers import create_initial_provider, create_provider_from_name, SUPPORTED_PROVIDERS
from src.providers.base_provider import BaseLLMProvider

class ProviderManager:

    def __init__(self, config: ConfigManager):
        self._config = config
        self._provider: Optional[BaseLLMProvider] = None

    def initialize_provider(self):
        # Always refresh config so runtime auth state updates are picked up.
        self._config = get_config(reload=True)
        self._provider = create_initial_provider(self._config)

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self._provider

    def switch_provider(self, provider_name: str):
        # Reload configuration before switching to pick up recent runtime state edits.
        self._config = get_config(reload=True)
        provider = create_provider_from_name(self._config, provider_name)
        if provider == None:
            raise ValueError(f"Unsupported provider: {provider_name}")
        else:
            self._provider = provider
    
