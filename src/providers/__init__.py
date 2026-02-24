from src.core.config_manager import ConfigManager
from src.providers.base_provider import BaseLLMProvider
from typing import Optional

SUPPORTED_PROVIDERS = ["anthropic", "gemini", "ollama"]

def list_providers() -> list[str]:
    return SUPPORTED_PROVIDERS.copy()

def create_initial_provider(config: ConfigManager) -> BaseLLMProvider:
    """
    Create an LLM provider instance based on configuration.

    Args:
        config: Loaded application configuration

    Returns:
        Initialized provider instance
    """
    provider_name = config.llm_provider.strip().lower()
    return _create_provider(config, provider_name)

def create_provider_from_name(config: ConfigManager, provider_name: str) -> Optional[BaseLLMProvider]:
    return _create_provider(config, provider_name)

def _create_provider(config: ConfigManager, provider_name: str) -> BaseLLMProvider:
    if provider_name in ("anthropic", "claude"):
        if not config.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in .env file")

        from src.providers.anthrophic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=config.anthropic_api_key,
            model=config.default_anthropic_model,
        )

    if provider_name in ("gemini", "google", "google-gemini"):
        if not config.google_api_key:
            raise ValueError("GOOGLE_API_KEY not found in .env file")

        from src.providers.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=config.google_api_key,
            model=config.default_gemini_model,
        )

    if provider_name in ("ollama"):
        if not config.ollama_base:
            raise ValueError("OLLAMA_BASE not found in .env file")

        from src.providers.ollama_provider import OllamaProvider

        return OllamaProvider(
            base_url=config.ollama_base,
            model=config.default_ollama_model,
            timeout_seconds=config.max_timeout_seconds,
        )
    
    return None
