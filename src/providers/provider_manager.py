from typing import Optional

from src.core.config_manager import ConfigManager
from src.providers import create_initial_provider, create_provider_from_name, SUPPORTED_PROVIDERS
from src.providers.base_provider import BaseLLMProvider

class ProviderManager:

    def __init__(self, config: ConfigManager):
        self._config = config

    def initialize_provider(self):
        self._provider = create_initial_provider(self._config)

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self._provider

    def switch_provider(self, provider_name: str):
        if provider_name not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider_name}")
    
        provider = create_provider_from_name(self._config, provider_name)
        if provider == None:
            raise ValueError(f"Unsupported provider: {provider_name}")
        else:
            self._provider = provider
    