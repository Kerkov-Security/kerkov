"""
Context compaction for conversation history management.

Summarizes older messages when token usage approaches the context window limit,
preserving actionable findings (credentials, IPs, flags, file paths, attack progress).
"""

from typing import Callable, Coroutine, List, Optional, Tuple

from src.context.token_tracker import TokenTracker
from src.core.logging.logger_factory import get_logger
from src.models.basic_models import Message

logger = get_logger("ContextCompactor")

# Async callable: takes a prompt string, returns summary text
AsyncSummarizeFn = Callable[[str], Coroutine[None, None, str]]

class ContextCompactor:
    """Compacts conversation history by summarizing older messages."""

    # Number of recent turns to always preserve verbatim
    PRESERVE_RECENT_TURNS: int = 6

    def __init__(
        self,
        token_tracker: TokenTracker,
        max_context_tokens: int,
        threshold_pct: float,
        summary_reserve_tokens: int,
    ):
        self.tracker = token_tracker
        self.max_tokens = max_context_tokens
        self.threshold = int(max_context_tokens * threshold_pct)
        self.summary_reserve = summary_reserve_tokens

    def usage_pct(self) -> float:
        """Return current token usage as a fraction of the context window."""
        if self.max_tokens == 0:
            return 0.0
        return self.tracker.total_tokens / self.max_tokens

    def should_compact(self) -> bool:
        """Check if total tokens exceed the compaction threshold."""
        over = self.tracker.total_tokens > self.threshold
        logger.info(
            "should_compact check: tokens=%d, threshold=%d, result=%s",
            self.tracker.total_tokens,
            self.threshold,
            over,
        )
        return over

    def select_messages_to_compact(
        self, history: List[Message]
    ) -> Optional[Tuple[int, int]]:
        """Return (start, end) range of messages to compact.

        Preserves the system prompt (index 0) and the most recent
        PRESERVE_RECENT_TURNS messages. Everything in between is
        eligible for compaction.
        """
        if len(history) <= self.PRESERVE_RECENT_TURNS + 1:
            logger.info(
                "Not enough messages to compact: %d <= %d",
                len(history),
                self.PRESERVE_RECENT_TURNS + 1,
            )
            return None

        start = 1  # Skip system prompt
        end = len(history) - self.PRESERVE_RECENT_TURNS

        if end <= start:
            logger.info("No compactable range: end=%d <= start=%d", end, start)
            return None

        logger.info("Selected messages [%d:%d] for compaction", start, end)
        return (start, end)

    def build_summarization_prompt(self, messages: List[Message]) -> str:
        """Build a prompt that asks the LLM to summarize the given messages.

        The summary preserves credentials, IPs, ports, flags, file paths,
        tool outcomes, attack chain progress, and current plans.
        """
        conversation_text = ""
        for msg in messages:
            role = msg.role.upper()
            content = msg.content or ""
            conversation_text += f"\n[{role}]: {content}\n"

        return (
            "Summarize the following conversation history into a concise context "
            "summary. This summary will replace the original messages in the "
            "conversation, so it must preserve ALL actionable information.\n\n"
            "YOU MUST PRESERVE:\n"
            "- All discovered credentials, API keys, tokens, passwords, flags\n"
            "- All IP addresses, ports, hostnames, domain names, URLs\n"
            "- All relevant file paths (read, written, or referenced)\n"
            "- Tools used and their key outcomes (success/failure + what was learned)\n"
            "- Attack chain progress: what was tried, what worked, what failed\n"
            "- Current hypotheses, plans, or next steps that were discussed\n"
            "- Any error messages or version numbers that informed decisions\n\n"
            "FORMAT: Use structured sections. Be concise but complete. "
            "No narrative prose.\n\n"
            f"CONVERSATION TO SUMMARIZE:\n{conversation_text}\n\n"
            "STRUCTURED SUMMARY:"
        )

    async def compact(
        self,
        history: List[Message],
        summarize_fn: AsyncSummarizeFn,
    ) -> Optional[str]:
        """Perform compaction on conversation history.

        Modifies history in-place. Returns the summary text, or None if
        compaction was not needed/possible.

        Args:
            history: The conversation history list (modified in-place)
            summarize_fn: Async function that takes a prompt string and
                         returns a summary string.
        """
        range_ = self.select_messages_to_compact(history)
        if range_ is None:
            logger.info("Compaction skipped: no eligible messages")
            return None

        start, end = range_
        messages_to_compact = history[start:end]
        logger.info(
            "Compacting %d messages [%d:%d]", len(messages_to_compact), start, end
        )

        prompt = self.build_summarization_prompt(messages_to_compact)
        summary = await summarize_fn(prompt)

        summary_message = Message(
            role="assistant",
            content=f"[CONTEXT SUMMARY - Earlier conversation compacted]\n\n{summary}",
        )

        # Replace compacted messages with summary
        self.tracker.remove_range(start, end)
        history[start:end] = [summary_message]

        # Track the new summary message's tokens
        summary_tokens = await self.tracker._count(summary_message.content)
        self.tracker.insert(start, summary_tokens)

        logger.info(
            "Compaction complete: replaced %d messages with summary (%d tokens)",
            len(messages_to_compact),
            summary_tokens,
        )
        return summary
