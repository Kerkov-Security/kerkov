"""
Base provider interface for LLM integrations.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.models.basic_models import LLMResponse, Message

class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, api_key: str, model: str, **kwargs):
        """
        Initialize the LLM provider.

        Args:
            api_key: API key for the provider
            model: Model identifier to use
            **kwargs: Additional provider-specific configuration
        """
        self.api_key = api_key
        self.model = model
        self.config = kwargs

    @abstractmethod
    async def complete(
        self,
        messages: List[Message],
        temperature: float,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a completion from the LLM.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0 to 1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters

        Returns:
            LLMResponse object with the completion
        """
        pass

    @abstractmethod
    async def count_tokens(self, text: str) -> int:
        """
        Count tokens in the given text.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
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
    