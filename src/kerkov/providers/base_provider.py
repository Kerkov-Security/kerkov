"""
Base provider interface for LLM integrations.
"""

from abc import ABC, abstractmethod
from copy import copy
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from kerkov.core.logging.logger_factory import get_logger
from kerkov.models.basic_models import LLMResponse, Message

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


_PROVIDER_NAMES = {
    alias: canonical
    for canonical, aliases in {
        "anthropic": ("AnthropicProvider", "claude"),
        "gemini": ("GeminiProvider", "google", "google-gemini"),
        "nvidia": ("NvidiaProvider", "nim", "nvidia-nim"),
        "openrouter": ("OpenRouterProvider", "open-router"),
        "deepseek": ("DeepSeekProvider",),
        "ollama": ("OllamaProvider",),
        "codex": ("CodexProvider", "openai-codex"),
    }.items()
    for alias in (canonical, *(name.lower() for name in aliases))
}


def normalize_provider_name(name: str) -> str:
    """Canonicalize built-in names and legacy class labels, preserving custom names."""
    name = name.strip()
    return _PROVIDER_NAMES.get(name.lower(), name)


def provider_name(provider: "BaseLLMProvider") -> str:
    """Resolve the provider's declared identity with a legacy class-name fallback."""
    info = provider.get_model_info()
    name = info.get("provider") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name.strip():
        name = type(provider).__name__
    return normalize_provider_name(name)


class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, api_key: str, model: str):
        """
        Initialize the LLM provider.

        Args:
            api_key: API key for the provider
            model: Model identifier to use
        """
        self.api_key = api_key
        self.model = model
        self.logger = get_logger(self.__class__.__name__)

    def set_model(self, model: str):
        self.logger.info(f"Switching model from '{self.model}' to '{model}'")
        self.model = model

    def fork(self, model: Optional[str] = None) -> "BaseLLMProvider":
        """Snapshot model selection without recreating transport or auth resources.

        Provider model state and local cache containers belong to the new object.
        SDK clients and auth managers are intentionally shared: rebuilding them
        can reconfigure SDK globals or race rotating credentials. Subclasses with
        additional mutable per-run state must override this method to isolate it.
        ``set_model`` preserves provider-specific local catalog validation without
        performing a remote model lookup or changing the source provider.
        """
        selected = self.model if model is None else model
        if not selected or any(char.isspace() for char in selected):
            raise ValueError("Model must be a nonempty ID without whitespace.")
        provider = copy(self)
        for name, value in vars(self).items():
            if isinstance(value, (dict, list, set)):
                setattr(provider, name, copy(value))
        provider.set_model(selected)
        return provider

    @property
    def caches_prompt_prefix(self) -> bool:
        """Whether the provider caches a stable prompt prefix across turns.

        When True, the engine keeps mutable working state (the findings ledger)
        out of the cached prefix by placing it at the tail of the request rather
        than near the front. Other providers keep the ledger high, where it is
        most salient. This is a placement hint; the provider handles any cache
        configuration or relies on the API's automatic prefix caching.
        """
        return False

    @abstractmethod
    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """
        Generate a completion from the LLM.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature. When None, the provider's own
                default is used (no value is forwarded to the API). Accepted
                ranges vary per provider — Anthropic/Ollama: 0.0–1.0,
                Gemini/OpenAI/DeepSeek: 0.0–2.0 (DeepSeek defaults to 1.0).
                DeepSeek thinking mode and Codex Responses ignore this value.
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools the LLM can use
            on_delta: Optional callback invoked with each chunk of assistant
                text as it streams. When None the provider accumulates the
                response internally and returns it whole (the agent path); the
                returned `LLMResponse` is identical either way.

        Returns:
            LLMResponse object with the completion
        """
        pass

    @abstractmethod
    def validate_connection(self) -> bool:
        """
        Validate the provider connection and credentials.

        Returns:
            True if connection is valid, False otherwise
        """
        pass

    @abstractmethod
    def list_models(self) -> List[str]:
        """
        List available models for the provider.

        Returns:
            List of model identifiers
        """
        pass

    @abstractmethod
    async def get_max_context_tokens(self) -> Optional[int]:
        """
        Return the maximum context window size (in tokens) for the current model.

        Implementations should cache per-model results and avoid blocking the
        event loop for any HTTP or SDK calls used to resolve the window.

        Returns:
            Maximum number of tokens the model's context window supports,
            or None if the provider cannot determine it.
        """
        pass

    @abstractmethod
    async def get_max_output_tokens(self) -> Optional[int]:
        """
        Return the maximum output (response) tokens the current model can generate per turn.

        Implementations should cache per-model results and avoid blocking the
        event loop for any HTTP or SDK calls used to resolve the cap.

        Returns:
            Maximum output tokens per response, or None if the provider does
            not use/expose this value.
        """
        pass

    def get_model_info(self) -> Dict[str, Any]:
        """
        Get information about the current model.

        Returns:
            Dictionary with model information
        """
        return {
            "provider": normalize_provider_name(type(self).__name__),
            "model": self.model,
        }
