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


if __name__ == "__main__":
    unittest.main()
