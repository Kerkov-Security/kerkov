"""
Context compaction for conversation history management.

Summarizes older messages when token usage approaches the context window limit,
preserving actionable findings (credentials, IPs, flags, file paths, attack progress).

One compaction runs in a fixed order: evict large tool results down to their
`artifact#N` handles, summarize the reasoning that is left, then — once the
summary block outgrows its cap — roll those summaries up. Summaries carry
`Message.summary_level`, which excludes them from every later compactable range,
so a summary is never re-summarized as if it were fresh narrative.
"""

from typing import Callable, Coroutine, List, Optional, Tuple

from kerkov.context.token_tracker import TokenTracker
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.tool_output import (
    build_evicted_tool_clip,
    build_evicted_tool_reference,
)
from kerkov.models.basic_models import Message

logger = get_logger("ContextCompactor")

# Async callable: takes a prompt string, returns summary text
AsyncSummarizeFn = Callable[[str], Coroutine[None, None, str]]


class ContextCompactor:
    """Compacts conversation history by summarizing older messages."""

    # Number of recent turns to always preserve verbatim
    PRESERVE_RECENT_TURNS: int = 6

    # A tool result smaller than this costs more in reference boilerplate than
    # evicting it reclaims, so it goes to the summarizer intact.
    EVICT_TOOL_RESULT_MIN_BYTES: int = 1024

    # How many summaries may sit at the head of history before they are rolled
    # up into one. Rollup stops at level 2: a rolled-up summary is never rolled
    # up again, so detail decays at most once.
    MAX_SUMMARIES_BEFORE_ROLLUP: int = 4
    MAX_SUMMARY_LEVEL: int = 2

    def __init__(
        self,
        token_tracker: TokenTracker,
        max_context_tokens: int,
        threshold_pct: float,
        summary_reserve_tokens: int,
        ledger_active: bool = False,
    ):
        self.tracker = token_tracker
        self.max_tokens = max_context_tokens
        self._threshold_pct = threshold_pct
        self.threshold = int(max_context_tokens * threshold_pct)
        self.summary_reserve = summary_reserve_tokens
        # When the findings ledger is on, confirmed facts are preserved outside
        # this summary, so the prompt can lean on narrative rather than
        # exhaustively re-listing creds/IPs/flags.
        self.ledger_active = ledger_active

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

        Preserves the system prompt (index 0), any summaries already produced,
        and the most recent PRESERVE_RECENT_TURNS messages. The end boundary is
        walked back past any trailing tool-result messages so a
        tool_use/tool_result group isn't split.

        Summaries form a contiguous block at the head — each new one is inserted
        after those already there — so skipping past it is enough to keep a
        summary from being fed back through `build_summarization_prompt` as if
        it were fresh narrative.
        """
        if len(history) <= self.PRESERVE_RECENT_TURNS + 1:
            return None

        start = self.first_compactable_index(history)
        end = len(history) - self.PRESERVE_RECENT_TURNS

        while end > start and end < len(history) and history[end].role == "tool":
            end -= 1

        if end <= start:
            return None

        return (start, end)

    def first_compactable_index(self, history: List[Message]) -> int:
        """Index of the first message past the system prompt and summary block."""
        index = 1
        while index < len(history) and history[index].summary_level is not None:
            index += 1
        return index

    def evict_tool_results(self, messages: List[Message]) -> Tuple[List[Message], int]:
        """Collapse large tool results so only reasoning reaches the summarizer.

        Returns a rewritten copy of the range and the number of bytes reclaimed.
        A message with an `artifact_ref` keeps its recall handle and nothing
        else; without one there is no handle, so a head/tail clip stands in.
        Every candidate over the floor is evicted, which is why there is no
        largest-first ordering here — the floor, not a budget, is the cutoff.
        """
        rewritten: List[Message] = []
        reclaimed = 0

        for message in messages:
            size = len(message.content.encode("utf-8"))
            if message.role != "tool" or size < self.EVICT_TOOL_RESULT_MIN_BYTES:
                rewritten.append(message)
                continue

            if message.artifact_ref is not None:
                content = build_evicted_tool_reference(message.artifact_ref)
            else:
                content = build_evicted_tool_clip(message.content)

            reclaimed += size - len(content.encode("utf-8"))
            rewritten.append(message.model_copy(update={"content": content}))

        if reclaimed:
            logger.info("Evicted %d bytes of tool output before summarizing", reclaimed)
        return rewritten, reclaimed

    def build_summarization_prompt(self, messages: List[Message]) -> str:
        """Build a prompt that asks the LLM to summarize the given messages."""
        conversation_text = ""
        for msg in messages:
            role = msg.role.upper()
            content = msg.content or ""
            conversation_text += f"\n[{role}]: {content}\n"

        if self.ledger_active:
            return (
                "Summarize the following conversation history into a concise "
                "context summary. This summary will replace the original "
                "messages in the conversation.\n\n"
                "Confirmed facts — credentials, hosts, ports, flags — are "
                "separately preserved in the findings ledger and stay in "
                "context, so you do NOT need to exhaustively re-list them here. "
                "Focus on the narrative and attack-chain reasoning:\n"
                "- What was tried, what worked, and what failed\n"
                "- Current hypotheses and planned next steps, with the reasons "
                "behind key decisions\n"
                "- Anything not obviously a ledger fact but still needed later: "
                "error messages, version strings, and the reasoning that "
                "informed choices\n\n"
                "Any artifact#N reference you see is a handle to tool output "
                "kept outside this conversation. Carry every one of them "
                "forward verbatim, next to what it was.\n\n"
                "FORMAT: Use structured sections. Be concise but complete.\n\n"
                f"CONVERSATION TO SUMMARIZE:\n{conversation_text}\n\n"
                "STRUCTURED SUMMARY:"
            )

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
            "- Any error messages or version numbers that informed decisions\n"
            "- Every artifact#N reference, verbatim, next to what it was: each "
            "is a handle to tool output kept outside this conversation\n\n"
            "FORMAT: Use structured sections. Be concise but complete. "
            "No narrative prose.\n\n"
            f"CONVERSATION TO SUMMARIZE:\n{conversation_text}\n\n"
            "STRUCTURED SUMMARY:"
        )

    def build_rollup_prompt(self, summaries: List[Message]) -> str:
        """Build a prompt that merges accumulated summaries into a single one."""
        summary_text = ""
        for index, msg in enumerate(summaries, start=1):
            summary_text += f"\n[SUMMARY {index}]: {msg.content}\n"

        return (
            "Merge the following context summaries, which are in chronological "
            "order, into ONE summary that replaces all of them.\n\n"
            "This is a merge, not a re-summarization: do not compress the "
            "material further than deduplication requires. Keep every "
            "credential, host, port, flag, file path and artifact#N reference "
            "that appears in any of them. Where later summaries correct or "
            "supersede earlier ones, keep the later account and drop the "
            "superseded one.\n\n"
            "FORMAT: Use structured sections, ordered chronologically.\n\n"
            f"SUMMARIES TO MERGE:\n{summary_text}\n\n"
            "MERGED SUMMARY:"
        )

    async def compact(
        self,
        history: List[Message],
        summarize_fn: AsyncSummarizeFn,
    ) -> Optional[List[Message]]:
        """Summarize the compactable range, then roll summaries up if they pile.

        Returns a new history list — the input is left untouched — or None if
        there was nothing to compact. The tracker is not re-estimated here:
        compaction only runs straight after a completion, so the next
        `response.usage` corrects the total before the next check.
        """
        range_ = self.select_messages_to_compact(history)
        if range_ is None:
            return None

        start, end = range_
        messages_to_compact = history[start:end]
        logger.info(
            "Compacting %d messages [%d:%d]", len(messages_to_compact), start, end
        )

        evicted, _ = self.evict_tool_results(messages_to_compact)
        prompt = self.build_summarization_prompt(evicted)
        summary = await summarize_fn(prompt)

        summary_message = Message(
            role="assistant",
            content=f"[CONTEXT SUMMARY - Earlier conversation compacted]\n\n{summary}",
            summary_level=1,
        )

        updated_history = history[:start] + [summary_message] + history[end:]

        logger.info(
            "Compaction complete: replaced %d messages with summary",
            len(messages_to_compact),
        )

        rolled_up = await self.roll_up_summaries(updated_history, summarize_fn)
        return rolled_up or updated_history

    async def roll_up_summaries(
        self,
        history: List[Message],
        summarize_fn: AsyncSummarizeFn,
    ) -> Optional[List[Message]]:
        """Merge the level-1 summaries into one when the block outgrows its cap.

        Returns a new history list, or None when there is nothing to roll up.
        Level-2 summaries are left verbatim — rolling one up again is the
        re-summarization this issue exists to stop, just one level higher — so a
        long engagement accumulates level-2 summaries slowly rather than
        collapsing into an ever-vaguer single blob.
        """
        block_end = self.first_compactable_index(history)
        if block_end - 1 <= self.MAX_SUMMARIES_BEFORE_ROLLUP:
            return None

        # Rollup replaces a run of level-1 summaries with the level-2 that
        # merges them, and new summaries land after it, so the block is always
        # level-2s then level-1s. Walking back from its end takes exactly the
        # level-1 run and can never swallow a level-2.
        first = block_end
        while (
            first > 1
            and (history[first - 1].summary_level or 0) < self.MAX_SUMMARY_LEVEL
        ):
            first -= 1
        if block_end - first < 2:
            return None

        to_roll = history[first:block_end]
        logger.info("Rolling %d summaries up into one", len(to_roll))

        merged = await summarize_fn(self.build_rollup_prompt(to_roll))
        merged_message = Message(
            role="assistant",
            content=f"[CONTEXT SUMMARY - Earlier summaries merged]\n\n{merged}",
            summary_level=self.MAX_SUMMARY_LEVEL,
        )
        return history[:first] + [merged_message] + history[block_end:]
