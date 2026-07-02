"""Tests for TokenTracker."""

import unittest

from src.context.token_tracker import TokenTracker


class RecordUsageTest(unittest.TestCase):
    def test_replaces_total_with_api_numbers(self):
        tracker = TokenTracker()

        tracker.record_usage(prompt_tokens=1200, completion_tokens=300)

        self.assertEqual(tracker.total_tokens, 1500)

    def test_defaults_completion_to_zero(self):
        tracker = TokenTracker()
        tracker.record_usage(prompt_tokens=500)
        self.assertEqual(tracker.total_tokens, 500)


class ResetTest(unittest.TestCase):
    def test_clears_total(self):
        tracker = TokenTracker()
        tracker.record_usage(1000, 1000)

        tracker.reset()

        self.assertEqual(tracker.total_tokens, 0)


if __name__ == "__main__":
    unittest.main()
