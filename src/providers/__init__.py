from src.core.config_manager import ConfigManager
from src.providers.base_provider import BaseLLMProvider

SUPPORTED_PROVIDERS = ["gemini", "ollama"]
DEFAULT_PROVIDER_MODELS = {
    "gemini": "gemini-2.5-flash",
    "ollama": "gpt-oss",
}

def list_providers() -> list[str]:
    return SUPPORTED_PROVIDERS.copy()

def get_default_model(provider_name: str) -> str:
    return DEFAULT_PROVIDER_MODELS.get(provider_name, "gemini-2.5-flash")

def create_provider(config: ConfigManager) -> BaseLLMProvider:
    """
    Create an LLM provider instance based on configuration.

    Args:
        config: Loaded application configuration

    Returns:
        Initialized provider instance
    """
    provider_name = (config.llm_provider or "gemini").strip().lower()

    if provider_name in ("gemini", "google", "google-gemini"):
        if not config.google_api_key:
            raise ValueError("GOOGLE_API_KEY not found in .env file")

        from src.providers.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=config.google_api_key,
            model=config.default_model,
        )

    if provider_name in ("ollama"):
        if not config.ollama_base:
            raise ValueError("OLLAMA_BASE not found in .env file")

        from src.providers.ollama_provider import OllamaProvider

        return OllamaProvider(
            base_url=config.ollama_base,
            model=config.default_model,
            timeout_seconds=config.max_timeout_seconds,
        )

    raise ValueError(f"Unsupported LLM provider: {config.llm_provider}")
