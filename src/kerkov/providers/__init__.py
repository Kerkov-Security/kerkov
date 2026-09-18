from typing import Optional

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import (
    CredentialManager,
    CredentialSpec,
    provider_credential_spec,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.providers.base_provider import BaseLLMProvider

logger = get_logger("ProviderFactory")

SUPPORTED_PROVIDERS = [
    "anthropic",
    "gemini",
    "nvidia",
    "openrouter",
    "deepseek",
    "ollama",
    "codex",
]


class MissingCredentialError(ValueError):
    """Raised when a provider needs an API key that is not configured."""

    def __init__(self, provider_name: str, spec: CredentialSpec):
        self.provider_name = provider_name
        self.spec = spec
        super().__init__(
            f"{spec.label} is not configured. Enter it securely when prompted."
        )


def list_providers() -> list[str]:
    return SUPPORTED_PROVIDERS.copy()


def create_initial_provider(
    config: ConfigManager,
    credentials: Optional[CredentialManager] = None,
) -> BaseLLMProvider:
    """
    Create an LLM provider instance based on configuration.

    Args:
        config: Loaded application configuration

    Returns:
        Initialized provider instance
    """
    provider_name = config.llm_provider.strip().lower()
    logger.info(f"Creating initial provider: '{provider_name}'")
    return _create_provider(config, provider_name, credentials)


def create_provider_from_name(
    config: ConfigManager,
    provider_name: str,
    credentials: Optional[CredentialManager] = None,
) -> Optional[BaseLLMProvider]:
    logger.info(f"Creating provider from name: '{provider_name}'")
    return _create_provider(config, provider_name, credentials)


def _create_provider(
    config: ConfigManager,
    provider_name: str,
    credentials: Optional[CredentialManager] = None,
) -> Optional[BaseLLMProvider]:
    if provider_name in ("anthropic", "claude"):
        api_key = _resolve_api_key(config.anthropic_api_key, provider_name, credentials)

        from kerkov.providers.anthrophic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=api_key,
            model=config.default_anthropic_model,
        )

    if provider_name in ("gemini", "google", "google-gemini"):
        api_key = _resolve_api_key(config.google_api_key, provider_name, credentials)

        from kerkov.providers.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=api_key,
            model=config.default_gemini_model,
        )

    if provider_name in ("ollama"):
        if not config.ollama_base:
            logger.error("OLLAMA_BASE not found in .env file")
            raise ValueError("OLLAMA_BASE not found in .env file")

        from kerkov.providers.ollama_provider import OllamaProvider

        return OllamaProvider(
            base_url=config.ollama_base,
            model=config.default_ollama_model,
            timeout_seconds=config.max_timeout_seconds,
        )

    if provider_name in ("nvidia", "nim", "nvidia-nim"):
        api_key = _resolve_api_key(config.nvidia_api_key, provider_name, credentials)

        from kerkov.providers.nvidia_provider import NvidiaProvider

        return NvidiaProvider(
            api_key=api_key,
            base_url=config.nvidia_base_url,
            model=config.default_nvidia_model,
            timeout_seconds=config.max_timeout_seconds,
        )

    if provider_name in ("openrouter", "open-router"):
        api_key = _resolve_api_key(
            config.openrouter_api_key, provider_name, credentials
        )

        from kerkov.providers.openrouter_provider import OpenRouterProvider

        return OpenRouterProvider(
            api_key=api_key,
            base_url=config.openrouter_base_url,
            model=config.default_openrouter_model,
            http_referer=config.openrouter_http_referer,
            app_title=config.openrouter_app_title,
            timeout_seconds=config.max_timeout_seconds,
        )

    if provider_name == "deepseek":
        api_key = _resolve_api_key(config.deepseek_api_key, provider_name, credentials)

        from kerkov.providers.deepseek_provider import DeepSeekProvider

        return DeepSeekProvider(
            api_key=api_key,
            base_url=config.deepseek_base_url,
            model=config.default_deepseek_model,
            timeout_seconds=config.max_timeout_seconds,
        )

    if provider_name in ("codex", "openai-codex"):
        has_auth = bool(config.codex_access_token or config.codex_oauth_refresh_token)
        if not has_auth:
            raise ValueError(
                "Codex auth is not configured. Select Codex with /provider to "
                "sign in with your browser."
            )

        from kerkov.providers.codex import CodexProvider

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


def _resolve_api_key(
    config_value: Optional[str],
    provider_name: str,
    credentials: Optional[CredentialManager],
) -> str:
    spec = provider_credential_spec(provider_name)
    if spec is None:
        raise ValueError(f"Provider '{provider_name}' has no credential specification.")

    value = credentials.get(spec) if credentials is not None else None
    value = value or config_value
    if not value:
        logger.warning("Credential is not configured for provider '%s'", provider_name)
        raise MissingCredentialError(provider_name, spec)
    return value
