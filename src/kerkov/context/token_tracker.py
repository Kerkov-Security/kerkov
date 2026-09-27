"""
Token tracking for conversation context management.

The tracker holds a single scalar updated authoritatively from the
provider's `response.usage` after each completion. That's it — the
API tells us the real size; we just remember it.

It also keeps a running total of everything billed, which is a different
number: `total_tokens` is how big the conversation is *now*, `cumulative_tokens`
is what it has cost to get here.
"""


class TokenTracker:
    """Scalar tracker for the current context size, fed from API responses."""

    def __init__(self) -> None:
        self.total_tokens: int = 0
        self.cumulative_tokens: int = 0

    def record_usage(self, prompt_tokens: int, completion_tokens: int = 0) -> None:
        """Replace the total with authoritative numbers from response.usage.

        After a completion, `prompt_tokens` is the size of history at call
        time, and `completion_tokens` is the assistant reply that's now
        appended to history. Their sum is the exact size of the conversation.
        """
        self.total_tokens = prompt_tokens + completion_tokens
        # Spend, not size: every call re-sends the history, so this keeps
        # climbing while `total_tokens` stays flat — and it does not drop when
        # a compaction shrinks the conversation.
        self.cumulative_tokens += prompt_tokens + completion_tokens

    def record_side_spend(self, prompt_tokens: int, completion_tokens: int = 0) -> None:
        """Bill tokens for a call that does not change context size.

        A summarizer or rollup request re-sends a slice of history and is paid
        for, but its prompt is not the size of the ongoing conversation — so it
        adds to spend without touching `total_tokens`, which drives compaction.
        """
        self.cumulative_tokens += prompt_tokens + completion_tokens

    def reset(self) -> None:
        """Zero both totals for a fresh conversation."""
        self.total_tokens = 0
        self.cumulative_tokens = 0
