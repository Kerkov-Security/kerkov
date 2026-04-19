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
        self._threshold_pct = threshold_pct
        self.threshold = int(max_context_tokens * threshold_pct)
        self.summary_reserve = summary_reserve_tokens

    def update_limits(self, max_context_tokens: int) -> None:
        """Update the context window size and recompute the compaction threshold."""
        self.max_tokens = max_context_tokens
        self.threshold = int(max_context_tokens * self._threshold_pct)

    def usage_pct(self) -> float:
        """Return current token usage as a fraction of the context window."""
        if self.max_tokens == 0:
            return 0.0
        return self.tracker.total_tokens / self.max_tokens

    def should_compact(self) -> bool:
        """Check if total tokens exceed the compaction threshold."""
        return self.tracker.total_tokens > self.threshold

    def select_messages_to_compact(
        self, history: List[Message]
    ) -> Optional[Tuple[int, int]]:
        """Return (start, end) range of messages to compact.

        Preserves the system prompt (index 0) and the most recent
        PRESERVE_RECENT_TURNS messages. The end boundary is walked back
        past any trailing tool-result messages so a tool_use/tool_result
        group isn't split.
        """
        if len(history) <= self.PRESERVE_RECENT_TURNS + 1:
            return None

        start = 1
        end = len(history) - self.PRESERVE_RECENT_TURNS

        while end > start and end < len(history) and history[end].role == "tool":
            end -= 1

        if end <= start:
            return None

        return (start, end)

    def build_summarization_prompt(self, messages: List[Message]) -> str:
        """Build a prompt that asks the LLM to summarize the given messages."""
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
    ) -> Optional[List[Message]]:
        """Perform compaction on conversation history.

        Modifies history in-place and re-estimates the tracker total from
        the new history. Returns the summary text, or None if compaction
        was not needed/possible.
        """
        range_ = self.select_messages_to_compact(history)
        if range_ is None:
            return None

        start, end = range_
        messages_to_compact = history[start:end]
        logger.info("Compacting %d messages [%d:%d]", len(messages_to_compact), start, end)

        prompt = self.build_summarization_prompt(messages_to_compact)
        summary = await summarize_fn(prompt)

        summary_message = Message(
            role="assistant",
            content=f"[CONTEXT SUMMARY - Earlier conversation compacted]\n\n{summary}",
        )

        updated_history = history[:start] + [summary_message] + history[end:]

        logger.info(
            "Compaction complete: replaced %d messages with summary", len(messages_to_compact)
        )
        return updated_history
