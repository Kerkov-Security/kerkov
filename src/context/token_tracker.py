"""
Token tracking for conversation context management.
"""

from typing import Callable, Coroutine, List

from src.models.basic_models import Message

# Async callable that takes a string and returns a token count
AsyncTokenCounter = Callable[[str], Coroutine[None, None, int]]


class TokenTracker:
    """Tracks token usage across conversation history."""

    def __init__(self, token_counter: AsyncTokenCounter):
        self._count = token_counter
        self._message_tokens: List[int] = []

    @property
    def total_tokens(self) -> int:
        return sum(self._message_tokens)

    @property
    def message_count(self) -> int:
        return len(self._message_tokens)

    async def track_message(self, message: Message) -> int:
        """Count tokens for a message and add to tracking. Returns token count."""
        tokens = await self._count_message(message)
        self._message_tokens.append(tokens)
        return tokens

    def remove_range(self, start: int, end: int) -> int:
        """Remove tracked tokens for message range [start, end). Returns tokens freed."""
        freed = sum(self._message_tokens[start:end])
        self._message_tokens[start:end] = []
        return freed

    def insert(self, index: int, tokens: int) -> None:
        """Insert a token count at a specific index (for summary replacement)."""
        self._message_tokens.insert(index, tokens)

    def reset(self) -> None:
        """Clear all tracked token counts."""
        self._message_tokens.clear()

    async def _count_message(self, message: Message) -> int:
        """Count tokens in a message including role and content."""
        text = message.role + " " + (message.content or "")
        return await self._count(text)
