
"""
Google Gemini provider implementation.
"""

from typing import List, Optional

import google.generativeai as genai

from src.models.basic_models import LLMResponse, Message, TokenUsage
from src.providers.base_provider import BaseLLMProvider


class GeminiProvider(BaseLLMProvider):
    """Google Gemini LLM provider."""

    def __init__(self, api_key: str, model: str = "gemini-pro", **kwargs):
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google API key
            model: Model name (default: gemini-pro)
            **kwargs: Additional configuration
        """
        super().__init__(api_key, model, **kwargs)
        genai.configure(api_key=api_key)
        self.client = genai.GenerativeModel(model)

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """
        Generate a completion from Gemini.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0 to 2.0 for Gemini)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional Gemini-specific parameters

        Returns:
            LLMResponse object with the completion
        """
        # Convert messages to Gemini format
        gemini_messages = self._convert_messages(messages)

        # Configure generation parameters
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            **kwargs
        )

        # Generate response
        response = await self.client.generate_content_async(
            gemini_messages,
            generation_config=generation_config
        )

        # Extract token usage if available
        usage = None
        if hasattr(response, 'usage_metadata'):
            usage = TokenUsage(
                prompt_tokens=response.usage_metadata.prompt_token_count,
                completion_tokens=response.usage_metadata.candidates_token_count,
                total_tokens=response.usage_metadata.total_token_count
            )

        return LLMResponse(
            content=response.text,
            model=self.model,
            usage=usage,
            raw_response=response
        )

    async def count_tokens(self, text: str) -> int:
        """
        Count tokens in the given text using Gemini's tokenizer.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        result = await self.client.count_tokens_async(text)
        return result.total_tokens

    def validate_connection(self) -> bool:
        """
        Validate the Gemini API connection and credentials.

        Returns:
            True if connection is valid, False otherwise
        """
        try:
            # Try to list models to validate API key
            list(genai.list_models())
            return True
        except Exception:
            return False

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """
        Convert standard messages to Gemini format.

        Gemini uses 'user' and 'model' roles, not 'assistant'.
        System messages are prepended to the first user message.

        Args:
            messages: List of standard Message objects

        Returns:
            List of Gemini-formatted message dictionaries
        """
        gemini_messages = []
        system_content = ""

        for msg in messages:
            if msg.role == "system":
                # Accumulate system messages
                system_content += msg.content + "\n"
            elif msg.role == "user":
                content = msg.content
                # Prepend system content to first user message
                if system_content:
                    content = system_content + content
                    system_content = ""
                gemini_messages.append({
                    "role": "user",
                    "parts": [content]
                })
            elif msg.role == "assistant":
                # Gemini uses 'model' instead of 'assistant'
                gemini_messages.append({
                    "role": "model",
                    "parts": [msg.content]
                })

        return gemini_messages