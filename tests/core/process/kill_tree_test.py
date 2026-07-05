import math
import unittest
from unittest.mock import patch

from kerkov.core.process.kill_tree import (
    DEFAULT_GRACE_MS,
    MAX_GRACE_MS,
    _normalize_grace_ms,
    kill_process_tree,
)

FAKE_PID = 12345


class TestNormalizeGraceMs(unittest.TestCase):

    def test_returns_default_for_none(self):
        assert _normalize_grace_ms(None) == DEFAULT_GRACE_MS

    def test_returns_default_for_nan(self):
        assert _normalize_grace_ms(math.nan) == DEFAULT_GRACE_MS

    def test_returns_default_for_string(self):
        assert _normalize_grace_ms("500") == DEFAULT_GRACE_MS

    def test_clamps_negative_to_zero(self):
        assert _normalize_grace_ms(-100) == 0

    def test_clamps_above_max(self):
        assert _normalize_grace_ms(MAX_GRACE_MS + 1000) == MAX_GRACE_MS

    def test_valid_int_passes_through(self):
        assert _normalize_grace_ms(5000) == 5000

    def test_float_is_truncated_to_int(self):
        assert _normalize_grace_ms(1500.9) == 1500

    def test_zero_is_valid(self):
        assert _normalize_grace_ms(0) == 0


class TestKillProcessTreeDispatch(unittest.TestCase):
    """The dispatcher validates the pid and delegates to the platform backend."""

    @patch("kerkov.core.process.kill_tree._backend")
    def test_noop_for_zero_pid(self, mock_backend):
        kill_process_tree(0)
        mock_backend.kill_tree.assert_not_called()

    @patch("kerkov.core.process.kill_tree._backend")
    def test_noop_for_negative_pid(self, mock_backend):
        kill_process_tree(-1)
        mock_backend.kill_tree.assert_not_called()

    @patch("kerkov.core.process.kill_tree._backend")
    def test_noop_for_non_int_pid(self, mock_backend):
        kill_process_tree("123")
        mock_backend.kill_tree.assert_not_called()

    @patch("kerkov.core.process.kill_tree._backend")
    def test_delegates_with_default_grace(self, mock_backend):
        kill_process_tree(FAKE_PID)
        mock_backend.kill_tree.assert_called_once_with(FAKE_PID, DEFAULT_GRACE_MS)

    @patch("kerkov.core.process.kill_tree._backend")
    def test_delegates_with_normalized_grace(self, mock_backend):
        kill_process_tree(FAKE_PID, grace_ms=MAX_GRACE_MS + 5000)
        mock_backend.kill_tree.assert_called_once_with(FAKE_PID, MAX_GRACE_MS)


if __name__ == "__main__":
    unittest.main()
