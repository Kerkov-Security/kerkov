import math
import signal
import unittest
from unittest.mock import patch, MagicMock

from src.core.process.kill_tree import (
    kill_process_tree,
    _force_kill,
    _is_process_alive,
    _is_process_alive_pg,
    _normalize_grace_ms,
    DEFAULT_GRACE_MS,
    MAX_GRACE_MS,
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


class TestIsProcessAlive(unittest.TestCase):

    @patch("src.core.process.kill_tree.os.kill")
    def test_returns_true_when_process_exists(self, mock_kill):
        mock_kill.return_value = None
        assert _is_process_alive(FAKE_PID) is True
        mock_kill.assert_called_once_with(FAKE_PID, 0)

    @patch("src.core.process.kill_tree.os.kill", side_effect=OSError)
    def test_returns_false_when_process_not_found(self, mock_kill):
        assert _is_process_alive(FAKE_PID) is False


class TestIsProcessAlivePg(unittest.TestCase):

    @patch("src.core.process.kill_tree.os.killpg")
    def test_returns_true_when_group_exists(self, mock_killpg):
        mock_killpg.return_value = None
        assert _is_process_alive_pg(FAKE_PID) is True
        mock_killpg.assert_called_once_with(FAKE_PID, 0)

    @patch("src.core.process.kill_tree.os.killpg", side_effect=OSError)
    def test_returns_false_when_group_not_found(self, mock_killpg):
        assert _is_process_alive_pg(FAKE_PID) is False


class TestForceKill(unittest.TestCase):

    @patch("src.core.process.kill_tree.os.killpg")
    @patch("src.core.process.kill_tree._is_process_alive_pg", return_value=True)
    def test_kills_process_group_when_alive(self, mock_alive_pg, mock_killpg):
        _force_kill(FAKE_PID)
        mock_killpg.assert_called_once_with(FAKE_PID, signal.SIGKILL)

    @patch("src.core.process.kill_tree.os.kill")
    @patch("src.core.process.kill_tree._is_process_alive", return_value=True)
    @patch("src.core.process.kill_tree.os.killpg", side_effect=OSError)
    @patch("src.core.process.kill_tree._is_process_alive_pg", return_value=True)
    def test_falls_back_to_direct_kill_when_group_fails(
        self, mock_alive_pg, mock_killpg, mock_alive, mock_kill
    ):
        _force_kill(FAKE_PID)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGKILL)

    @patch("src.core.process.kill_tree.os.kill")
    @patch("src.core.process.kill_tree.os.killpg")
    @patch("src.core.process.kill_tree._is_process_alive", return_value=False)
    @patch("src.core.process.kill_tree._is_process_alive_pg", return_value=False)
    def test_noop_when_process_already_dead(
        self, mock_alive_pg, mock_alive, mock_killpg, mock_kill
    ):
        _force_kill(FAKE_PID)
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    @patch("src.core.process.kill_tree.os.kill", side_effect=OSError)
    @patch("src.core.process.kill_tree._is_process_alive", return_value=True)
    @patch("src.core.process.kill_tree._is_process_alive_pg", return_value=False)
    def test_ignores_oserror_on_direct_kill(self, mock_alive_pg, mock_alive, mock_kill):
        _force_kill(FAKE_PID)


class TestKillProcessTree(unittest.TestCase):

    # --- Input validation ---

    @patch("src.core.process.kill_tree.os.killpg")
    def test_noop_for_zero_pid(self, mock_killpg):
        kill_process_tree(0)
        mock_killpg.assert_not_called()

    @patch("src.core.process.kill_tree.os.killpg")
    def test_noop_for_negative_pid(self, mock_killpg):
        kill_process_tree(-1)
        mock_killpg.assert_not_called()

    @patch("src.core.process.kill_tree.os.killpg")
    def test_noop_for_non_int_pid(self, mock_killpg):
        kill_process_tree("123")
        mock_killpg.assert_not_called()

    # --- SIGTERM phase ---

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.killpg")
    def test_sends_sigterm_to_process_group(self, mock_killpg, mock_timer):
        kill_process_tree(FAKE_PID)
        mock_killpg.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch(
        "src.core.process.kill_tree.os.killpg", side_effect=ProcessLookupError
    )
    def test_returns_early_if_process_already_gone(self, mock_killpg, mock_timer):
        kill_process_tree(FAKE_PID)
        mock_timer.assert_not_called()

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.kill")
    @patch(
        "src.core.process.kill_tree.os.killpg", side_effect=PermissionError
    )
    def test_falls_back_to_direct_sigterm_on_permission_error(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_process_tree(FAKE_PID)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.kill")
    @patch("src.core.process.kill_tree.os.killpg", side_effect=OSError)
    def test_falls_back_to_direct_sigterm_on_generic_oserror(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_process_tree(FAKE_PID)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.kill", side_effect=OSError)
    @patch("src.core.process.kill_tree.os.killpg", side_effect=OSError)
    def test_returns_if_direct_sigterm_also_fails(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_process_tree(FAKE_PID)
        mock_timer.assert_not_called()

    # --- SIGKILL scheduling ---

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.killpg")
    def test_schedules_sigkill_timer_with_default_grace(self, mock_killpg, mock_timer):
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        kill_process_tree(FAKE_PID)

        mock_timer.assert_called_once_with(
            DEFAULT_GRACE_MS / 1000.0, _force_kill, args=[FAKE_PID]
        )
        mock_instance.start.assert_called_once()

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.killpg")
    def test_schedules_sigkill_timer_with_custom_grace(self, mock_killpg, mock_timer):
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        kill_process_tree(FAKE_PID, grace_ms=5000)

        mock_timer.assert_called_once_with(5.0, _force_kill, args=[FAKE_PID])

    @patch("src.core.process.kill_tree._force_kill")
    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.killpg")
    def test_immediate_force_kill_when_grace_is_zero(
        self, mock_killpg, mock_timer, mock_force_kill
    ):
        kill_process_tree(FAKE_PID, grace_ms=0)
        mock_force_kill.assert_called_once_with(FAKE_PID)
        mock_timer.assert_not_called()

    @patch("src.core.process.kill_tree.threading.Timer")
    @patch("src.core.process.kill_tree.os.killpg")
    def test_timer_is_daemon(self, mock_killpg, mock_timer):
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        kill_process_tree(FAKE_PID)

        assert mock_instance.daemon is True
