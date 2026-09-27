"""Tests for TokenTracker."""

import unittest

from kerkov.context.token_tracker import TokenTracker


class RecordUsageTest(unittest.TestCase):
    def test_replaces_total_with_api_numbers(self):
        tracker = TokenTracker()

        tracker.record_usage(prompt_tokens=1200, completion_tokens=300)

        self.assertEqual(tracker.total_tokens, 1500)

    def test_defaults_completion_to_zero(self):
        tracker = TokenTracker()
        tracker.record_usage(prompt_tokens=500)
        self.assertEqual(tracker.total_tokens, 500)


class RecordSideSpendTest(unittest.TestCase):
    def test_adds_to_cumulative_without_changing_size(self):
        tracker = TokenTracker()
        tracker.record_usage(prompt_tokens=1200, completion_tokens=300)

        tracker.record_side_spend(prompt_tokens=700, completion_tokens=50)

        # Size is still what the last real completion reported.
        self.assertEqual(tracker.total_tokens, 1500)
        # Spend includes the side call.
        self.assertEqual(tracker.cumulative_tokens, 2250)

    def test_defaults_completion_to_zero(self):
        tracker = TokenTracker()
        tracker.record_side_spend(prompt_tokens=400)
        self.assertEqual(tracker.cumulative_tokens, 400)
        self.assertEqual(tracker.total_tokens, 0)


class ResetTest(unittest.TestCase):
    def test_clears_total(self):
        tracker = TokenTracker()
        tracker.record_usage(1000, 1000)

        tracker.reset()

        self.assertEqual(tracker.total_tokens, 0)

    def test_clears_cumulative(self):
        tracker = TokenTracker()
        tracker.record_usage(1000, 1000)
        tracker.record_side_spend(500)

        tracker.reset()

        self.assertEqual(tracker.cumulative_tokens, 0)


if __name__ == "__main__":
    unittest.main()
