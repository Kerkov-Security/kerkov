"""Tests for ContextCompactor."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.token_tracker import TokenTracker
from kerkov.models.basic_models import Message


def _run(coro):
    return asyncio.run(coro)


def _tracker_at(total: int) -> TokenTracker:
    tracker = TokenTracker()
    tracker.total_tokens = total
    return tracker


def _create_history(n: int) -> list:
    messages = [Message(role="system", content="system prompt")]
    for i in range(1, n):
        role = "user" if i % 2 == 1 else "assistant"
        messages.append(Message(role=role, content=f"message {i}"))
    return messages


def _summary(text: str = "earlier stuff", level: int = 1) -> Message:
    return Message(role="assistant", content=text, summary_level=level)


def _tool_message(content: str, artifact_ref=None) -> Message:
    return Message(
        role="tool", content=content, call_id="c1", artifact_ref=artifact_ref
    )


def _make_compactor(tracker: TokenTracker, max_tokens: int = 1000) -> ContextCompactor:
    return ContextCompactor(
        token_tracker=tracker,
        max_context_tokens=max_tokens,
        threshold_pct=0.65,
        summary_reserve_tokens=200,
    )


class ShouldCompactTest(unittest.TestCase):
    def test_true_when_over_threshold(self):
        self.assertTrue(_make_compactor(_tracker_at(700)).should_compact())

    def test_false_when_under_threshold(self):
        self.assertFalse(_make_compactor(_tracker_at(300)).should_compact())

    def test_false_at_exact_threshold(self):
        # threshold is int(1000 * 0.65) = 650; 650 is NOT over.
        self.assertFalse(_make_compactor(_tracker_at(650)).should_compact())


class SelectMessagesToCompactTest(unittest.TestCase):
    def test_preserves_system_prompt_and_recent_turns(self):
        compactor = _make_compactor(_tracker_at(0))
        history = _create_history(12)

        result = compactor.select_messages_to_compact(history)

        self.assertEqual(result, (1, 6))

    def test_returns_none_when_not_enough_messages(self):
        compactor = _make_compactor(_tracker_at(0))
        self.assertIsNone(compactor.select_messages_to_compact(_create_history(7)))

    def test_does_not_split_tool_call_group_at_boundary(self):
        compactor = _make_compactor(_tracker_at(0))
        history = [
            Message(role="system", content="sys"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="u2"),
            Message(role="assistant", content="a2-tc"),
            Message(role="tool", content="t1", call_id="c1"),
            Message(role="tool", content="t2", call_id="c2"),
            Message(role="tool", content="t3", call_id="c3"),
            Message(role="assistant", content="a3"),
            Message(role="user", content="u3"),
            Message(role="assistant", content="a4"),
            Message(role="user", content="u4"),
            Message(role="assistant", content="a5"),
        ]

        start, end = compactor.select_messages_to_compact(history)

        self.assertEqual(start, 1)
        self.assertEqual(end, 4)
        self.assertNotEqual(history[end].role, "tool")

    def test_returns_none_when_walk_back_eliminates_range(self):
        compactor = _make_compactor(_tracker_at(0))
        history = [
            Message(role="system", content="sys"),
            Message(role="assistant", content="a1-tc"),
            Message(role="tool", content="t1", call_id="c1"),
            Message(role="assistant", content="a2"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a3"),
            Message(role="user", content="u2"),
            Message(role="assistant", content="a4"),
        ]

        self.assertIsNone(compactor.select_messages_to_compact(history))


class CompactTest(unittest.TestCase):
    def test_replaces_messages_with_summary(self):
        tracker = TokenTracker()
        tracker.record_usage(prompt_tokens=900)
        compactor = _make_compactor(tracker)
        history = _create_history(10)
        original = list(history)

        summarize_fn = AsyncMock(return_value="Summary of earlier conversation")

        updated = _run(compactor.compact(history, summarize_fn))

        self.assertIsNotNone(updated)
        # system + summary + 6 preserved
        self.assertEqual(len(updated), 8)
        self.assertIn("[CONTEXT SUMMARY", updated[1].content)
        self.assertIn("Summary of earlier conversation", updated[1].content)
        # input history must not be mutated
        self.assertEqual(history, original)

    def test_returns_none_when_not_enough_messages(self):
        compactor = _make_compactor(_tracker_at(0))
        history = _create_history(5)
        summarize_fn = AsyncMock()

        result = _run(compactor.compact(history, summarize_fn))

        self.assertIsNone(result)
        summarize_fn.assert_not_called()

    def test_preserves_system_prompt(self):
        tracker = TokenTracker()
        compactor = _make_compactor(tracker)
        history = _create_history(10)

        updated = _run(compactor.compact(history, AsyncMock(return_value="summary")))

        self.assertIsNotNone(updated)
        self.assertEqual(updated[0].role, "system")
        self.assertEqual(updated[0].content, "system prompt")

    def test_does_not_mutate_input_history(self):
        tracker = TokenTracker()
        tracker.record_usage(prompt_tokens=900)
        compactor = _make_compactor(tracker)
        history = _create_history(10)
        original_ids = [id(m) for m in history]
        original_len = len(history)

        _run(compactor.compact(history, AsyncMock(return_value="summary")))

        self.assertEqual(len(history), original_len)
        self.assertEqual([id(m) for m in history], original_ids)


class SummaryExclusionTest(unittest.TestCase):
    """Summaries are marked and skipped, so they are never re-summarized."""

    def test_compact_marks_its_summary(self):
        compactor = _make_compactor(_tracker_at(900))
        updated = _run(
            compactor.compact(_create_history(10), AsyncMock(return_value="s"))
        )
        self.assertEqual(updated[1].summary_level, 1)

    def test_range_starts_after_leading_summaries(self):
        compactor = _make_compactor(_tracker_at(0))
        history = [Message(role="system", content="sys"), _summary(), _summary()]
        history += [
            Message(role="user" if i % 2 else "assistant", content=f"m{i}")
            for i in range(10)
        ]

        start, end = compactor.select_messages_to_compact(history)

        self.assertEqual(start, 3)
        self.assertTrue(all(m.summary_level is None for m in history[start:end]))

    def test_returns_none_when_only_summaries_remain(self):
        compactor = _make_compactor(_tracker_at(0))
        history = [Message(role="system", content="sys")] + [
            _summary() for _ in range(3)
        ]
        history += [Message(role="user", content=f"m{i}") for i in range(6)]

        self.assertIsNone(compactor.select_messages_to_compact(history))

    def test_second_compaction_does_not_re_summarize_the_first_summary(self):
        compactor = _make_compactor(_tracker_at(900))
        history = _create_history(14)

        first_fn = AsyncMock(return_value="FIRST-SUMMARY-TEXT")
        history = _run(compactor.compact(history, first_fn))
        history += [Message(role="user", content=f"later {i}") for i in range(8)]

        second_fn = AsyncMock(return_value="SECOND-SUMMARY-TEXT")
        _run(compactor.compact(history, second_fn))

        second_prompt = second_fn.await_args.args[0]
        self.assertNotIn("FIRST-SUMMARY-TEXT", second_prompt)


class EvictToolResultsTest(unittest.TestCase):
    def test_artifact_backed_result_becomes_a_bare_reference(self):
        compactor = _make_compactor(_tracker_at(0))

        evicted, reclaimed = compactor.evict_tool_results(
            [_tool_message("x" * 20_000, artifact_ref=42)]
        )

        self.assertIn("artifact#42", evicted[0].content)
        self.assertNotIn("xxxx", evicted[0].content)
        self.assertGreater(reclaimed, 19_000)

    def test_result_without_an_artifact_becomes_a_head_tail_clip(self):
        compactor = _make_compactor(_tracker_at(0))

        evicted, _ = compactor.evict_tool_results(
            [_tool_message("HEAD" + "x" * 20_000 + "TAIL")]
        )

        content = evicted[0].content
        self.assertNotIn("artifact#", content)
        self.assertIn("HEAD", content)
        self.assertIn("TAIL", content)
        self.assertIn("characters elided", content)

    def test_small_tool_result_is_left_alone(self):
        compactor = _make_compactor(_tracker_at(0))
        message = _tool_message("small output", artifact_ref=7)

        evicted, reclaimed = compactor.evict_tool_results([message])

        self.assertEqual(evicted[0].content, "small output")
        self.assertEqual(reclaimed, 0)

    def test_non_tool_messages_are_untouched(self):
        compactor = _make_compactor(_tracker_at(0))
        reasoning = Message(role="assistant", content="y" * 20_000)

        evicted, reclaimed = compactor.evict_tool_results([reasoning])

        self.assertEqual(evicted[0].content, reasoning.content)
        self.assertEqual(reclaimed, 0)

    def test_does_not_mutate_the_input_messages(self):
        compactor = _make_compactor(_tracker_at(0))
        message = _tool_message("x" * 20_000, artifact_ref=1)

        compactor.evict_tool_results([message])

        self.assertEqual(len(message.content), 20_000)

    def test_compact_evicts_the_blob_but_keeps_the_reasoning(self):
        compactor = _make_compactor(_tracker_at(900))
        history = [
            Message(role="system", content="sys"),
            Message(role="user", content="scan it"),
            Message(role="assistant", content="REASONING-ABOUT-THE-SCAN"),
            _tool_message("BLOBBLOB" * 3_000, artifact_ref=9),
            Message(role="assistant", content="a2"),
        ] + [Message(role="user", content=f"m{i}") for i in range(6)]

        summarize_fn = AsyncMock(return_value="summary")
        _run(compactor.compact(history, summarize_fn))

        prompt = summarize_fn.await_args.args[0]
        self.assertIn("REASONING-ABOUT-THE-SCAN", prompt)
        self.assertNotIn("BLOBBLOB", prompt)
        self.assertIn("artifact#9", prompt)


class RollUpSummariesTest(unittest.TestCase):
    def _history_with(self, summaries):
        return (
            [Message(role="system", content="sys")]
            + summaries
            + [Message(role="user", content=f"m{i}") for i in range(6)]
        )

    def test_returns_none_below_the_cap(self):
        compactor = _make_compactor(_tracker_at(0))
        history = self._history_with([_summary(f"s{i}") for i in range(4)])

        result = _run(compactor.roll_up_summaries(history, AsyncMock()))

        self.assertIsNone(result)

    def test_merges_level_one_summaries_past_the_cap(self):
        compactor = _make_compactor(_tracker_at(0))
        history = self._history_with([_summary(f"s{i}") for i in range(5)])

        merge_fn = AsyncMock(return_value="MERGED")
        result = _run(compactor.roll_up_summaries(history, merge_fn))

        summaries = [m for m in result if m.summary_level is not None]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].summary_level, 2)
        self.assertIn("MERGED", summaries[0].content)
        self.assertEqual(result[0].role, "system")

    def test_rollup_prompt_carries_every_summary(self):
        compactor = _make_compactor(_tracker_at(0))
        history = self._history_with([_summary(f"S{i}-TEXT") for i in range(5)])

        merge_fn = AsyncMock(return_value="MERGED")
        _run(compactor.roll_up_summaries(history, merge_fn))

        prompt = merge_fn.await_args.args[0]
        for i in range(5):
            self.assertIn(f"S{i}-TEXT", prompt)

    def test_level_two_summaries_are_never_rolled_up_again(self):
        compactor = _make_compactor(_tracker_at(0))
        history = self._history_with([_summary(f"s{i}", level=2) for i in range(5)])

        merge_fn = AsyncMock()
        result = _run(compactor.roll_up_summaries(history, merge_fn))

        self.assertIsNone(result)
        merge_fn.assert_not_called()

    def test_rollup_leaves_existing_level_two_summaries_in_place(self):
        compactor = _make_compactor(_tracker_at(0))
        history = self._history_with(
            [_summary("OLD-ROLLUP", level=2)] + [_summary(f"s{i}") for i in range(4)]
        )

        result = _run(compactor.roll_up_summaries(history, AsyncMock(return_value="M")))

        summaries = [m for m in result if m.summary_level is not None]
        self.assertEqual([m.summary_level for m in summaries], [2, 2])
        self.assertIn("OLD-ROLLUP", summaries[0].content)

    def test_compact_rolls_up_once_the_block_outgrows_the_cap(self):
        compactor = _make_compactor(_tracker_at(900))
        history = (
            [Message(role="system", content="sys")]
            + [_summary(f"s{i}") for i in range(4)]
            + [Message(role="user", content=f"m{i}") for i in range(10)]
        )

        result = _run(compactor.compact(history, AsyncMock(return_value="text")))

        summaries = [m for m in result if m.summary_level is not None]
        self.assertEqual([m.summary_level for m in summaries], [2])


class UsageReactivityTest(unittest.TestCase):
    """Compaction stays keyed to authoritative usage — no byte/char estimator."""

    def test_should_compact_ignores_history_size(self):
        compactor = _make_compactor(_tracker_at(0))
        huge = [Message(role="user", content="x" * 1_000_000) for _ in range(50)]

        self.assertFalse(compactor.should_compact())
        self.assertEqual(len(huge), 50)

    def test_should_compact_follows_the_tracker(self):
        tracker = TokenTracker()
        compactor = _make_compactor(tracker)

        self.assertFalse(compactor.should_compact())
        tracker.record_usage(prompt_tokens=900)
        self.assertTrue(compactor.should_compact())


class UsagePctTest(unittest.TestCase):
    def test_returns_fraction_of_context_used(self):
        compactor = _make_compactor(_tracker_at(500), max_tokens=1000)
        self.assertAlmostEqual(compactor.usage_pct(), 0.5)

    def test_returns_zero_when_empty(self):
        compactor = _make_compactor(_tracker_at(0))
        self.assertEqual(compactor.usage_pct(), 0.0)

    def test_returns_zero_when_max_tokens_is_zero(self):
        compactor = ContextCompactor(
            token_tracker=_tracker_at(100),
            max_context_tokens=0,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        self.assertEqual(compactor.usage_pct(), 0.0)


class UpdateLimitsTest(unittest.TestCase):
    def test_updates_max_and_recomputes_threshold(self):
        compactor = _make_compactor(_tracker_at(0))

        compactor.update_limits(2000)

        self.assertEqual(compactor.max_tokens, 2000)
        self.assertEqual(compactor.threshold, 1300)

    def test_should_compact_reflects_new_limits(self):
        compactor = _make_compactor(_tracker_at(700))
        self.assertTrue(compactor.should_compact())

        compactor.update_limits(2000)

        self.assertFalse(compactor.should_compact())


class BuildSummarizationPromptTest(unittest.TestCase):
    def test_includes_message_content(self):
        compactor = _make_compactor(_tracker_at(0))
        messages = [
            Message(role="user", content="scan the target"),
            Message(role="assistant", content="running nmap"),
        ]

        prompt = compactor.build_summarization_prompt(messages)

        self.assertIn("scan the target", prompt)
        self.assertIn("running nmap", prompt)
        self.assertIn("[USER]", prompt)
        self.assertIn("[ASSISTANT]", prompt)
        self.assertIn("STRUCTURED SUMMARY:", prompt)

    def test_default_prompt_keeps_exhaustive_preservation_mandate(self):
        compactor = _make_compactor(_tracker_at(0))
        prompt = compactor.build_summarization_prompt(
            [Message(role="user", content="x")]
        )
        self.assertIn("YOU MUST PRESERVE", prompt)
        self.assertNotIn("findings ledger", prompt)

    def test_ledger_active_softens_prompt_and_mentions_ledger(self):
        compactor = ContextCompactor(
            token_tracker=_tracker_at(0),
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
            ledger_active=True,
        )
        prompt = compactor.build_summarization_prompt(
            [Message(role="user", content="x")]
        )
        self.assertNotIn("YOU MUST PRESERVE", prompt)
        self.assertIn("findings ledger", prompt)


if __name__ == "__main__":
    unittest.main()
