from src.core.config_manager import ConfigManager
from src.core.logging.logger_factory import get_logger
from src.providers.base_provider import BaseLLMProvider
from typing import Optional

logger = get_logger("ProviderFactory")

SUPPORTED_PROVIDERS = ["anthropic", "gemini", "ollama", "codex"]

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
    logger.info(f"Creating initial provider: '{provider_name}'")
    return _create_provider(config, provider_name)

def create_provider_from_name(config: ConfigManager, provider_name: str) -> Optional[BaseLLMProvider]:
    logger.info(f"Creating provider from name: '{provider_name}'")
    return _create_provider(config, provider_name)

def _create_provider(config: ConfigManager, provider_name: str) -> Optional[BaseLLMProvider]:
    if provider_name in ("anthropic", "claude"):
        if not config.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY not found in .env file")
            raise ValueError("ANTHROPIC_API_KEY not found in .env file")

        from src.providers.anthrophic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=config.anthropic_api_key,
            model=config.default_anthropic_model,
        )

    if provider_name in ("gemini", "google", "google-gemini"):
        if not config.google_api_key:
            logger.error("GOOGLE_API_KEY not found in .env file")
            raise ValueError("GOOGLE_API_KEY not found in .env file")

        from src.providers.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=config.google_api_key,
            model=config.default_gemini_model,
        )

    if provider_name in ("ollama"):
        if not config.ollama_base:
            logger.error("OLLAMA_BASE not found in .env file")
            raise ValueError("OLLAMA_BASE not found in .env file")

        from src.providers.ollama_provider import OllamaProvider

        return OllamaProvider(
            base_url=config.ollama_base,
            model=config.default_ollama_model,
            timeout_seconds=config.max_timeout_seconds,
        )

    if provider_name in ("codex", "openai-codex"):
        has_auth = bool(config.codex_access_token or config.codex_oauth_refresh_token)
        if not has_auth:
            raise ValueError(
                "Codex auth is not configured. Run /codex-login to populate the Codex runtime state file."
            )

        from src.providers.codex import CodexProvider

        return CodexProvider(
            model=config.default_codex_model,
            base_url=config.codex_base_url or "https://chatgpt.com/backend-api",
            access_token=config.codex_access_token,
            account_id=config.codex_account_id,
            timeout_seconds=config.max_timeout_seconds,
            config=config,
        )
    

    logger.warning(f"Unsupported provider name: '{provider_name}'")
    return None
