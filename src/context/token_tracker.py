"""
Token tracking for conversation context management.

The tracker holds a single scalar updated authoritatively from the
provider's `response.usage` after each completion. That's it — the
API tells us the real size; we just remember it.
"""

class TokenTracker:
    """Scalar tracker for the current context size, fed from API responses."""

    def __init__(self) -> None:
        self.total_tokens: int = 0

    def record_usage(self, prompt_tokens: int, completion_tokens: int = 0) -> None:
        """Replace the total with authoritative numbers from response.usage.

        After a completion, `prompt_tokens` is the size of history at call
        time, and `completion_tokens` is the assistant reply that's now
        appended to history. Their sum is the exact size of the conversation.
        """
        self.total_tokens = prompt_tokens + completion_tokens

    def reset(self) -> None:
        """Zero the total. Next recorded usage will repopulate it."""
        self.total_tokens = 0
