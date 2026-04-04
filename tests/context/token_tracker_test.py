"""Tests for TokenTracker."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from src.context.token_tracker import TokenTracker
from src.models.basic_models import Message


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _create_mock_counter(token_count: int = 10) -> AsyncMock:
    return AsyncMock(return_value=token_count)


class TokenTrackerTrackMessageTest(unittest.TestCase):
    """Tests for TokenTracker.track_message()."""

    def test_tracks_single_message(self):
        counter = _create_mock_counter(25)
        tracker = TokenTracker(token_counter=counter)

        msg = Message(role="user", content="hello world")
        result = _run(tracker.track_message(msg))

        self.assertEqual(result, 25)
        self.assertEqual(tracker.total_tokens, 25)
        self.assertEqual(tracker.message_count, 1)

    def test_tracks_cumulative_tokens(self):
        counter = AsyncMock(side_effect=[10, 20, 30])
        tracker = TokenTracker(token_counter=counter)

        _run(tracker.track_message(Message(role="user", content="a")))
        _run(tracker.track_message(Message(role="assistant", content="b")))
        _run(tracker.track_message(Message(role="user", content="c")))

        self.assertEqual(tracker.total_tokens, 60)
        self.assertEqual(tracker.message_count, 3)

    def test_passes_role_and_content_to_counter(self):
        counter = _create_mock_counter(5)
        tracker = TokenTracker(token_counter=counter)

        _run(tracker.track_message(Message(role="user", content="test input")))

        counter.assert_called_once_with("user test input")

    def test_handles_empty_content(self):
        counter = _create_mock_counter(2)
        tracker = TokenTracker(token_counter=counter)

        _run(tracker.track_message(Message(role="assistant", content="")))

        counter.assert_called_once_with("assistant ")
        self.assertEqual(tracker.total_tokens, 2)


class TokenTrackerRemoveRangeTest(unittest.TestCase):
    """Tests for TokenTracker.remove_range()."""

    def test_removes_range_and_returns_freed(self):
        counter = AsyncMock(side_effect=[10, 20, 30, 40])
        tracker = TokenTracker(token_counter=counter)

        for content in ["a", "b", "c", "d"]:
            _run(tracker.track_message(Message(role="user", content=content)))

        freed = tracker.remove_range(1, 3)

        self.assertEqual(freed, 50)  # 20 + 30
        self.assertEqual(tracker.total_tokens, 50)  # 10 + 40
        self.assertEqual(tracker.message_count, 2)

    def test_remove_empty_range(self):
        counter = _create_mock_counter(10)
        tracker = TokenTracker(token_counter=counter)
        _run(tracker.track_message(Message(role="user", content="a")))

        freed = tracker.remove_range(0, 0)

        self.assertEqual(freed, 0)
        self.assertEqual(tracker.total_tokens, 10)


class TokenTrackerInsertTest(unittest.TestCase):
    """Tests for TokenTracker.insert()."""

    def test_insert_at_position(self):
        counter = AsyncMock(side_effect=[10, 30])
        tracker = TokenTracker(token_counter=counter)

        _run(tracker.track_message(Message(role="user", content="a")))
        _run(tracker.track_message(Message(role="user", content="c")))

        tracker.insert(1, 20)

        self.assertEqual(tracker.total_tokens, 60)
        self.assertEqual(tracker.message_count, 3)


class TokenTrackerResetTest(unittest.TestCase):
    """Tests for TokenTracker.reset()."""

    def test_reset_clears_all(self):
        counter = _create_mock_counter(10)
        tracker = TokenTracker(token_counter=counter)

        _run(tracker.track_message(Message(role="user", content="a")))
        _run(tracker.track_message(Message(role="user", content="b")))

        tracker.reset()

        self.assertEqual(tracker.total_tokens, 0)
        self.assertEqual(tracker.message_count, 0)


class TokenTrackerEmptyTest(unittest.TestCase):
    """Tests for TokenTracker initial state."""

    def test_empty_tracker(self):
        counter = _create_mock_counter()
        tracker = TokenTracker(token_counter=counter)

        self.assertEqual(tracker.total_tokens, 0)
        self.assertEqual(tracker.message_count, 0)


if __name__ == "__main__":
    unittest.main()
