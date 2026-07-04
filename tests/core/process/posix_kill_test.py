import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX kill backend (signals/process groups)"
)

if sys.platform != "win32":
    import signal

    from kerkov.core.process.posix_kill import (
        _force_kill,
        _is_process_alive,
        _is_process_alive_pg,
        kill_tree,
    )

FAKE_PID = 12345


class TestIsProcessAlive(unittest.TestCase):

    @patch("kerkov.core.process.posix_kill.os.kill")
    def test_returns_true_when_process_exists(self, mock_kill):
        mock_kill.return_value = None
        assert _is_process_alive(FAKE_PID) is True
        mock_kill.assert_called_once_with(FAKE_PID, 0)

    @patch("kerkov.core.process.posix_kill.os.kill", side_effect=OSError)
    def test_returns_false_when_process_not_found(self, mock_kill):
        assert _is_process_alive(FAKE_PID) is False


class TestIsProcessAlivePg(unittest.TestCase):

    @patch("kerkov.core.process.posix_kill.os.killpg")
    def test_returns_true_when_group_exists(self, mock_killpg):
        mock_killpg.return_value = None
        assert _is_process_alive_pg(FAKE_PID) is True
        mock_killpg.assert_called_once_with(FAKE_PID, 0)

    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=OSError)
    def test_returns_false_when_group_not_found(self, mock_killpg):
        assert _is_process_alive_pg(FAKE_PID) is False


class TestForceKill(unittest.TestCase):

    @patch("kerkov.core.process.posix_kill.os.killpg")
    @patch("kerkov.core.process.posix_kill._is_process_alive_pg", return_value=True)
    def test_kills_process_group_when_alive(self, mock_alive_pg, mock_killpg):
        _force_kill(FAKE_PID)
        mock_killpg.assert_called_once_with(FAKE_PID, signal.SIGKILL)

    @patch("kerkov.core.process.posix_kill.os.kill")
    @patch("kerkov.core.process.posix_kill._is_process_alive", return_value=True)
    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=OSError)
    @patch("kerkov.core.process.posix_kill._is_process_alive_pg", return_value=True)
    def test_falls_back_to_direct_kill_when_group_fails(
        self, mock_alive_pg, mock_killpg, mock_alive, mock_kill
    ):
        _force_kill(FAKE_PID)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGKILL)

    @patch("kerkov.core.process.posix_kill.os.kill")
    @patch("kerkov.core.process.posix_kill.os.killpg")
    @patch("kerkov.core.process.posix_kill._is_process_alive", return_value=False)
    @patch("kerkov.core.process.posix_kill._is_process_alive_pg", return_value=False)
    def test_noop_when_process_already_dead(
        self, mock_alive_pg, mock_alive, mock_killpg, mock_kill
    ):
        _force_kill(FAKE_PID)
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    @patch("kerkov.core.process.posix_kill.os.kill", side_effect=OSError)
    @patch("kerkov.core.process.posix_kill._is_process_alive", return_value=True)
    @patch("kerkov.core.process.posix_kill._is_process_alive_pg", return_value=False)
    def test_ignores_oserror_on_direct_kill(self, mock_alive_pg, mock_alive, mock_kill):
        _force_kill(FAKE_PID)


class TestKillTree(unittest.TestCase):

    # --- SIGTERM phase ---

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.killpg")
    def test_sends_sigterm_to_process_group(self, mock_killpg, mock_timer):
        kill_tree(FAKE_PID, 3000)
        mock_killpg.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=ProcessLookupError)
    def test_returns_early_if_process_already_gone(self, mock_killpg, mock_timer):
        kill_tree(FAKE_PID, 3000)
        mock_timer.assert_not_called()

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.kill")
    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=PermissionError)
    def test_falls_back_to_direct_sigterm_on_permission_error(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_tree(FAKE_PID, 3000)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.kill")
    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=OSError)
    def test_falls_back_to_direct_sigterm_on_generic_oserror(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_tree(FAKE_PID, 3000)
        mock_kill.assert_called_once_with(FAKE_PID, signal.SIGTERM)

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.kill", side_effect=OSError)
    @patch("kerkov.core.process.posix_kill.os.killpg", side_effect=OSError)
    def test_returns_if_direct_sigterm_also_fails(
        self, mock_killpg, mock_kill, mock_timer
    ):
        kill_tree(FAKE_PID, 3000)
        mock_timer.assert_not_called()

    # --- SIGKILL scheduling ---

    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.killpg")
    def test_schedules_sigkill_timer(self, mock_killpg, mock_timer):
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        kill_tree(FAKE_PID, 5000)

        mock_timer.assert_called_once_with(5.0, _force_kill, args=[FAKE_PID])
        mock_instance.start.assert_called_once()
        assert mock_instance.daemon is True

    @patch("kerkov.core.process.posix_kill._force_kill")
    @patch("kerkov.core.process.posix_kill.threading.Timer")
    @patch("kerkov.core.process.posix_kill.os.killpg")
    def test_immediate_force_kill_when_grace_is_zero(
        self, mock_killpg, mock_timer, mock_force_kill
    ):
        kill_tree(FAKE_PID, 0)
        mock_force_kill.assert_called_once_with(FAKE_PID)
        mock_timer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
