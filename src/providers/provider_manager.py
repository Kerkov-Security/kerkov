from typing import Optional

from src.core.config_manager import ConfigManager
from src.core.logging.logger_factory import get_logger
from src.providers import create_initial_provider, create_provider_from_name, SUPPORTED_PROVIDERS
from src.providers.base_provider import BaseLLMProvider

logger = get_logger("ProviderManager")

class ProviderManager:

    def __init__(self, config: ConfigManager):
        self._config = config

    def initialize_provider(self):
        logger.info(f"Initializing provider from config (provider={self._config.llm_provider})")
        self._provider = create_initial_provider(self._config)
        logger.info(f"Provider initialized: {self._provider.__class__.__name__}")

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self._provider

    def switch_provider(self, provider_name: str):
        logger.info(f"Switching provider to '{provider_name}'")
        provider = create_provider_from_name(self._config, provider_name)
        if provider == None:
            logger.error(f"Unsupported provider: {provider_name}")
            raise ValueError(f"Unsupported provider: {provider_name}")
        else:
            self._provider = provider
            logger.info(f"Switched to {provider.__class__.__name__}")
    