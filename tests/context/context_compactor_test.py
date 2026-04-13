"""Tests for ContextCompactor."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from src.context.context_compactor import ContextCompactor
from src.context.token_tracker import TokenTracker
from src.models.basic_models import Message


def _run(coro):
    return asyncio.run(coro)


def _create_tracker(token_count: int = 10) -> TokenTracker:
    return TokenTracker(token_counter=AsyncMock(return_value=token_count))


def _create_history(n: int) -> list:
    """Create a conversation history with n messages (first is system)."""
    messages = [Message(role="system", content="system prompt")]
    for i in range(1, n):
        role = "user" if i % 2 == 1 else "assistant"
        messages.append(Message(role=role, content=f"message {i}"))
    return messages


class ShouldCompactTest(unittest.TestCase):
    """Tests for ContextCompactor.should_compact()."""

    def test_returns_true_when_over_threshold(self):
        tracker = _create_tracker(100)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # Track enough messages to exceed threshold (650 tokens)
        for _ in range(7):
            _run(tracker.track_message(Message(role="user", content="x")))

        self.assertTrue(compactor.should_compact())

    def test_returns_false_when_under_threshold(self):
        tracker = _create_tracker(100)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # Track few messages to stay under threshold
        for _ in range(3):
            _run(tracker.track_message(Message(role="user", content="x")))

        self.assertFalse(compactor.should_compact())

    def test_returns_false_when_exactly_at_threshold(self):
        tracker = _create_tracker(65)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # 10 messages * 65 tokens = 650, which equals threshold (not over)
        for _ in range(10):
            _run(tracker.track_message(Message(role="user", content="x")))

        self.assertFalse(compactor.should_compact())


class SelectMessagesToCompactTest(unittest.TestCase):
    """Tests for ContextCompactor.select_messages_to_compact()."""

    def test_preserves_system_prompt_and_recent_turns(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # 12 messages: system + 11 conversation messages
        history = _create_history(12)

        result = compactor.select_messages_to_compact(history)

        self.assertIsNotNone(result)
        start, end = result
        self.assertEqual(start, 1)  # Skip system prompt
        self.assertEqual(end, 6)    # 12 - 6 (PRESERVE_RECENT_TURNS)

    def test_returns_none_when_not_enough_messages(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # Only 7 messages (system + 6), no room to compact
        history = _create_history(7)

        result = compactor.select_messages_to_compact(history)

        self.assertIsNone(result)

    def test_returns_none_when_too_few_messages(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = _create_history(3)

        result = compactor.select_messages_to_compact(history)

        self.assertIsNone(result)


class CompactTest(unittest.TestCase):
    """Tests for ContextCompactor.compact()."""

    def test_replaces_messages_with_summary(self):
        counter = AsyncMock(return_value=10)
        tracker = TokenTracker(token_counter=counter)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = _create_history(10)
        for msg in history:
            _run(tracker.track_message(msg))

        summarize_fn = AsyncMock(return_value="Summary of earlier conversation")

        summary = _run(compactor.compact(history, summarize_fn))

        self.assertEqual(summary, "Summary of earlier conversation")
        # Should have: system + 1 summary + 6 preserved = 8 messages
        self.assertEqual(len(history), 8)
        self.assertIn("[CONTEXT SUMMARY", history[1].content)

    def test_returns_none_when_not_enough_messages(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = _create_history(5)
        summarize_fn = AsyncMock()

        result = _run(compactor.compact(history, summarize_fn))

        self.assertIsNone(result)
        summarize_fn.assert_not_called()

    def test_updates_token_tracker(self):
        counter = AsyncMock(return_value=10)
        tracker = TokenTracker(token_counter=counter)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = _create_history(10)
        for msg in history:
            _run(tracker.track_message(msg))

        initial_count = tracker.message_count
        self.assertEqual(initial_count, 10)

        summarize_fn = AsyncMock(return_value="short summary")
        _run(compactor.compact(history, summarize_fn))

        # 10 messages - 3 compacted + 1 summary = 8
        self.assertEqual(tracker.message_count, 8)

    def test_preserves_system_prompt(self):
        counter = AsyncMock(return_value=10)
        tracker = TokenTracker(token_counter=counter)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = _create_history(10)
        for msg in history:
            _run(tracker.track_message(msg))

        summarize_fn = AsyncMock(return_value="summary")
        _run(compactor.compact(history, summarize_fn))

        self.assertEqual(history[0].role, "system")
        self.assertEqual(history[0].content, "system prompt")


class UsagePctTest(unittest.TestCase):
    """Tests for ContextCompactor.usage_pct()."""

    def test_returns_fraction_of_context_used(self):
        tracker = _create_tracker(100)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        for _ in range(5):
            _run(tracker.track_message(Message(role="user", content="x")))

        self.assertAlmostEqual(compactor.usage_pct(), 0.5)

    def test_returns_zero_when_empty(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )

        self.assertEqual(compactor.usage_pct(), 0.0)

    def test_returns_zero_when_max_tokens_is_zero(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=0,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )

        self.assertEqual(compactor.usage_pct(), 0.0)


class UpdateLimitsTest(unittest.TestCase):
    """Tests for ContextCompactor.update_limits()."""

    def test_updates_max_and_recomputes_threshold(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )

        compactor.update_limits(2000)

        self.assertEqual(compactor.max_tokens, 2000)
        self.assertEqual(compactor.threshold, 1300)

    def test_should_compact_reflects_new_limits(self):
        tracker = _create_tracker(100)
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # 7 * 100 = 700 > 650 (old threshold) but < 1300 (new threshold)
        for _ in range(7):
            _run(tracker.track_message(Message(role="user", content="x")))
        self.assertTrue(compactor.should_compact())

        compactor.update_limits(2000)

        self.assertFalse(compactor.should_compact())


class BuildSummarizationPromptTest(unittest.TestCase):
    """Tests for ContextCompactor.build_summarization_prompt()."""

    def test_includes_message_content(self):
        tracker = _create_tracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
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
