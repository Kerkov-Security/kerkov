"""
Base provider interface for LLM integrations.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.core.logging.logger_factory import get_logger
from src.models.basic_models import LLMResponse, Message

if TYPE_CHECKING:
    from src.tools.base_tool import BaseTool

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

    @abstractmethod
    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
    ) -> LLMResponse:
        """
        Generate a completion from the LLM.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature. When None, the provider's own
                default is used (no value is forwarded to the API). Accepted
                ranges vary per provider — Anthropic/Ollama: 0.0–1.0,
                Gemini/OpenAI: 0.0–2.0. Codex Responses ignores this value.
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools the LLM can use

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
            "provider": self.__class__.__name__,
            "model": self.model,
        }
    
