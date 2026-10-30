"""
Context compaction for conversation history management.

Summarizes older messages when token usage approaches the context window limit,
preserving actionable findings (credentials, IPs, flags, file paths, attack progress).

One compaction runs in a fixed order: evict large tool outputs down to their
`artifact#N` handles, summarize the reasoning that is left, then — once the
summary block outgrows its caps — roll those summaries up. Summaries carry
`Message.summary_level`, which excludes them from every later compactable range,
so a summary is never re-summarized as if it were fresh narrative. Being
excluded, the block is also the one part of history compaction cannot reclaim,
so both it and the artifact index appended to each summary are bounded.
"""

import re
from typing import Callable, Coroutine, List, Optional, Tuple

from kerkov.context.token_tracker import TokenTracker
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.tool_output import (
    build_evicted_tool_clip,
    build_evicted_tool_reference,
    split_tool_output,
)
from kerkov.models.basic_models import Message

logger = get_logger("ContextCompactor")

# Async callable: takes a prompt string, returns summary text
AsyncSummarizeFn = Callable[[str], Coroutine[None, None, str]]

ARTIFACT_REF_PATTERN = re.compile(r"artifact#(\d+)")


class ContextCompactor:
    """Compacts conversation history by summarizing older messages."""

    # Maximum number of recent messages to preserve before applying the byte cap.
    PRESERVE_RECENT_TURNS: int = 6

    # Byte ceiling on that window. Count alone exempts the newest messages
    # whatever they cost, so one oversized result — a `recall(full=true)`
    # expansion, which is deliberately not clipped on the way into history —
    # would pin the context for six messages with eviction forbidden to touch
    # it. A whole window of normalized tool results fits well inside this, so
    # the budget only binds when something abnormally large is in it.
    PRESERVE_RECENT_BYTES: int = 65536

    # A tool result smaller than this costs more in reference boilerplate than
    # evicting it reclaims, so it goes to the summarizer intact.
    EVICT_TOOL_RESULT_MIN_BYTES: int = 1024

    # How many summaries may sit at the head of history before the level-1 run
    # is merged into one level-2, and how many level-2s may accumulate before
    # they in turn are merged. The second cap is what bounds the block: the
    # summary block is excluded from every compactable range, so an unbounded
    # top level would eventually fill the context with nothing left to compact.
    MAX_SUMMARIES_BEFORE_ROLLUP: int = 4
    MAX_ROLLED_UP_SUMMARIES: int = 4
    MAX_SUMMARY_LEVEL: int = 2

    # Ceiling on the deterministic artifact index appended to a summary. It
    # rides along outside compaction like the summaries themselves, so it needs
    # its own bound; the newest handles are the ones still worth recalling.
    MAX_INDEXED_ARTIFACTS: int = 60

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
        and the recent window `preserve_boundary` measures. The end boundary is
        walked back past any trailing tool-result messages so a
        tool_use/tool_result group isn't split. If walking back exceeds the byte
        budget or consumes the whole range, walk forward past the group instead
        when a later message shows that the model has read its results.

        Summaries form a contiguous block at the head — each new one is inserted
        after those already there — so skipping past it is enough to keep a
        summary from being fed back through `build_summarization_prompt` as if
        it were fresh narrative.
        """
        start = self.first_compactable_index(history)
        boundary = self.preserve_boundary(history)
        end = boundary

        while end > start and end < len(history) and history[end].role == "tool":
            end -= 1

        # Walking back can pull a large call or more results into the window,
        # including messages beyond the count limit preserve_boundary checked.
        # If that exceeds the byte cap, compact the whole exchange once the
        # model has read it. A trailing exchange still stays intact.
        if (
            end < boundary
            and sum(self._message_size_bytes(message) for message in history[end:])
            > self.PRESERVE_RECENT_BYTES
        ):
            forward = self._forward_group_boundary(history, boundary)
            if forward is not None:
                end = forward

        if end <= start:
            forward = self._forward_group_boundary(history, boundary)
            if forward is None or forward <= start:
                return None
            end = forward

        return (start, end)

    def preserve_boundary(self, history: List[Message]) -> int:
        """Index of the oldest message the recent window keeps.

        Preserves up to PRESERVE_RECENT_TURNS messages, but stops early once
        PRESERVE_RECENT_BYTES is spent on content and raw replay blocks, so an
        oversized message ages out of the window after one turn rather than six.
        Selection adjusts this boundary to keep tool exchanges intact. The
        newest message is kept whatever it costs: compaction runs directly after
        tool results are appended, so dropping them here would answer the model's
        own call with a summary of it.
        """
        if not history:
            return 0

        index = len(history) - 1
        budget = self.PRESERVE_RECENT_BYTES - self._message_size_bytes(history[index])

        while index > 0 and len(history) - index < self.PRESERVE_RECENT_TURNS:
            size = self._message_size_bytes(history[index - 1])
            if size > budget:
                break
            budget -= size
            index -= 1

        return index

    @staticmethod
    def _message_size_bytes(message: Message) -> int:
        """Conservative UTF-8 footprint including provider replay payloads.

        Pydantic serializes both SDK block models and saved dictionaries. Count
        content alongside raw blocks even if a provider replays overlapping
        text; tool arguments and reasoning must never count as free history.
        """
        return len(
            message.model_dump_json(
                include={"content", "raw_blocks"}, exclude_none=True
            ).encode("utf-8")
        )

    def _forward_group_boundary(
        self, history: List[Message], boundary: int
    ) -> Optional[int]:
        """First non-tool index at or after `boundary`, or None if there is none.

        Walking the boundary back out of a tool-result run can consume the whole
        compactable range; walking forward past the run keeps the same group
        intact on the other side. A run that reaches the end of history has no
        boundary to offer — anything preserved after it would be a tool_result
        whose tool_use was compacted away, which providers reject.
        """
        index = boundary
        while index < len(history) and history[index].role == "tool":
            index += 1

        return index if index < len(history) else None

    def first_compactable_index(self, history: List[Message]) -> int:
        """Index of the first message past the system prompt and summary block."""
        index = 1
        while index < len(history) and history[index].summary_level is not None:
            index += 1
        return index

    def evict_tool_results(self, messages: List[Message]) -> Tuple[List[Message], int]:
        """Collapse large tool outputs so only reasoning reaches the summarizer.

        Returns a rewritten copy of the range and the number of bytes reclaimed.
        Only the output body is replaced — by a recall handle when the message
        has an `artifact_ref`, by a head/tail clip when it does not. The header
        `format_tool_result` wrote stays: status, error and metadata live only in
        the message, since the artifact holds `ToolResult.output` alone, so
        replacing the whole thing would destroy them. The floor is measured
        against that body for the same reason — a metadata-heavy result with a
        short output has nothing worth evicting.

        Every candidate over the floor is evicted, which is why there is no
        largest-first ordering here — the floor, not a budget, is the cutoff.
        """
        rewritten: List[Message] = []
        reclaimed = 0

        for message in messages:
            if message.role != "tool":
                rewritten.append(message)
                continue

            header, body = split_tool_output(message.content)
            if len(body.encode("utf-8")) < self.EVICT_TOOL_RESULT_MIN_BYTES:
                rewritten.append(message)
                continue

            if message.artifact_ref is not None:
                replacement = build_evicted_tool_reference(message.artifact_ref)
            else:
                replacement = build_evicted_tool_clip(body)

            content = header + replacement
            reclaimed += len(message.content.encode("utf-8")) - len(
                content.encode("utf-8")
            )
            rewritten.append(message.model_copy(update={"content": content}))

        if reclaimed:
            logger.info("Evicted %d bytes of tool output before summarizing", reclaimed)
        return rewritten, reclaimed

    def collect_artifact_refs(self, messages: List[Message]) -> List[int]:
        """Every artifact id reachable from a range, stamped or written inline."""
        refs = set()
        for message in messages:
            if message.artifact_ref is not None:
                refs.add(message.artifact_ref)
            refs.update(
                int(match) for match in ARTIFACT_REF_PATTERN.findall(message.content)
            )
        return sorted(refs)

    def build_artifact_index(self, refs: List[int]) -> str:
        """Render the handles a summary must carry, whatever the model returned.

        The prompts ask for these verbatim, but the replacement history is built
        from model-generated text alone: an omitted handle would be the last
        copy of it, since the messages holding it are about to be dropped.
        """
        if not refs:
            return ""

        kept = refs[-self.MAX_INDEXED_ARTIFACTS :]
        dropped = len(refs) - len(kept)
        listed = ", ".join(f"artifact#{ref}" for ref in kept)
        omitted = f" ({dropped} older handles omitted)" if dropped else ""
        return (
            f"\n\n[Tool output from the compacted messages, still retrievable "
            f"with recall(query='artifact#N'): {listed}{omitted}.]"
        )

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
            logger.warning(
                "Nothing to compact: %d messages, %d already summarized, "
                "recent window starts at %d",
                len(history),
                self.first_compactable_index(history) - 1,
                self.preserve_boundary(history),
            )
            return None

        start, end = range_
        messages_to_compact = history[start:end]
        logger.info(
            "Compacting %d messages [%d:%d]", len(messages_to_compact), start, end
        )

        evicted, _ = self.evict_tool_results(messages_to_compact)
        prompt = self.build_summarization_prompt(evicted)
        summary = await summarize_fn(prompt)

        index = self.build_artifact_index(self.collect_artifact_refs(evicted))
        summary_message = Message(
            role="assistant",
            content=(
                f"[CONTEXT SUMMARY - Earlier conversation compacted]\n\n"
                f"{summary}{index}"
            ),
            summary_level=1,
            context_only=any(message.context_only for message in messages_to_compact),
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
        """Merge the summary block back down when it outgrows its caps.

        Returns a new history list, or None when there is nothing to roll up.
        Two bounded passes: the level-1 run merges into one level-2, and the
        level-2s merge among themselves once they pass their own cap. The
        second pass is what keeps the block finite — without it a long
        engagement accumulates a level-2 every couple of compactions, and since
        `first_compactable_index` excludes all of them it can reach the context
        threshold with no compactable range left. Detail therefore decays more
        than once at the head, which the merge prompt keeps as slow as it can.
        """
        updated = history
        rolled = False

        merged = await self._merge_level_one_run(updated, summarize_fn)
        if merged is not None:
            updated = merged
            rolled = True

        merged = await self._merge_rolled_up_run(updated, summarize_fn)
        if merged is not None:
            updated = merged
            rolled = True

        return updated if rolled else None

    def _summary_block_split(self, history: List[Message]) -> Tuple[int, int]:
        """(end of the level-2 run, end of the summary block).

        Rollup replaces a run of level-1 summaries with the level-2 that merges
        them, and new summaries land after it, so the block is always level-2s
        then level-1s and one index separates the two runs.
        """
        block_end = self.first_compactable_index(history)
        split = 1
        while (
            split < block_end
            and (history[split].summary_level or 0) >= self.MAX_SUMMARY_LEVEL
        ):
            split += 1
        return split, block_end

    async def _merge_level_one_run(
        self,
        history: List[Message],
        summarize_fn: AsyncSummarizeFn,
    ) -> Optional[List[Message]]:
        """Merge the trailing level-1 summaries once the block passes its cap."""
        split, block_end = self._summary_block_split(history)
        if block_end - 1 <= self.MAX_SUMMARIES_BEFORE_ROLLUP:
            return None
        if block_end - split < 2:
            return None

        return await self._replace_with_merge(history, split, block_end, summarize_fn)

    async def _merge_rolled_up_run(
        self,
        history: List[Message],
        summarize_fn: AsyncSummarizeFn,
    ) -> Optional[List[Message]]:
        """Merge the level-2 summaries among themselves once they pass their cap."""
        split, _ = self._summary_block_split(history)
        if split - 1 <= self.MAX_ROLLED_UP_SUMMARIES:
            return None

        return await self._replace_with_merge(history, 1, split, summarize_fn)

    async def _replace_with_merge(
        self,
        history: List[Message],
        first: int,
        end: int,
        summarize_fn: AsyncSummarizeFn,
    ) -> List[Message]:
        """Replace history[first:end] with the one level-2 summary that merges it."""
        to_roll = history[first:end]
        logger.info("Rolling %d summaries up into one", len(to_roll))

        merged = await summarize_fn(self.build_rollup_prompt(to_roll))
        index = self.build_artifact_index(self.collect_artifact_refs(to_roll))
        merged_message = Message(
            role="assistant",
            content=(
                f"[CONTEXT SUMMARY - Earlier summaries merged]\n\n{merged}{index}"
            ),
            summary_level=self.MAX_SUMMARY_LEVEL,
            context_only=any(message.context_only for message in to_roll),
        )
        return history[:first] + [merged_message] + history[end:]
