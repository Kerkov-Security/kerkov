import unittest
from unittest.mock import MagicMock, patch

from kerkov.core.process import windows_kill

FAKE_PID = 12345


class TestTaskkill(unittest.TestCase):

    @patch("kerkov.core.process.windows_kill.subprocess.run")
    def test_force_includes_f_flag(self, mock_run):
        windows_kill._taskkill(FAKE_PID, force=True)
        args = mock_run.call_args[0][0]
        assert args[:2] == ["taskkill", "/F"]
        assert "/T" in args
        assert str(FAKE_PID) in args

    @patch("kerkov.core.process.windows_kill.subprocess.run")
    def test_graceful_omits_f_flag(self, mock_run):
        windows_kill._taskkill(FAKE_PID, force=False)
        args = mock_run.call_args[0][0]
        assert "/F" not in args
        assert args[0] == "taskkill"
        assert "/T" in args

    @patch("kerkov.core.process.windows_kill.subprocess.run", side_effect=OSError)
    def test_suppresses_os_error(self, mock_run):
        windows_kill._taskkill(FAKE_PID, force=True)  # should not raise


class TestForceKill(unittest.TestCase):

    @patch("kerkov.core.process.windows_kill._taskkill")
    @patch("kerkov.core.process.windows_kill._is_process_alive", return_value=False)
    def test_noop_when_already_dead(self, mock_alive, mock_taskkill):
        windows_kill._force_kill(FAKE_PID)
        mock_taskkill.assert_not_called()

    @patch("kerkov.core.process.windows_kill.psutil", None)
    @patch("kerkov.core.process.windows_kill._taskkill")
    @patch("kerkov.core.process.windows_kill._is_process_alive", return_value=True)
    def test_uses_taskkill_force(self, mock_alive, mock_taskkill):
        windows_kill._force_kill(FAKE_PID)
        mock_taskkill.assert_called_once_with(FAKE_PID, force=True)

    @patch("kerkov.core.process.windows_kill._taskkill")
    @patch("kerkov.core.process.windows_kill._is_process_alive", return_value=True)
    def test_psutil_kill_sweep(self, mock_alive, mock_taskkill):
        child = MagicMock()
        parent = MagicMock()
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = parent
        parent.children.return_value = [child]
        with patch("kerkov.core.process.windows_kill.psutil", fake_psutil):
            windows_kill._force_kill(FAKE_PID)
        child.kill.assert_called_once()
        parent.kill.assert_called_once()


class TestKillTree(unittest.TestCase):

    @patch("kerkov.core.process.windows_kill._force_kill")
    @patch("kerkov.core.process.windows_kill.threading.Timer")
    def test_immediate_force_when_grace_zero(self, mock_timer, mock_force):
        windows_kill.kill_tree(FAKE_PID, 0)
        mock_force.assert_called_once_with(FAKE_PID)
        mock_timer.assert_not_called()

    @patch("kerkov.core.process.windows_kill._graceful_kill")
    @patch("kerkov.core.process.windows_kill.threading.Timer")
    def test_graceful_then_schedules_force(self, mock_timer, mock_graceful):
        mock_instance = MagicMock()
        mock_timer.return_value = mock_instance

        windows_kill.kill_tree(FAKE_PID, 3000)

        mock_graceful.assert_called_once_with(FAKE_PID)
        mock_timer.assert_called_once_with(
            3.0, windows_kill._force_kill, args=[FAKE_PID]
        )
        mock_instance.start.assert_called_once()
        assert mock_instance.daemon is True


class TestGracefulKill(unittest.TestCase):

    @patch("kerkov.core.process.windows_kill.psutil", None)
    @patch("kerkov.core.process.windows_kill._taskkill")
    def test_falls_back_to_taskkill_without_psutil(self, mock_taskkill):
        windows_kill._graceful_kill(FAKE_PID)
        mock_taskkill.assert_called_once_with(FAKE_PID, force=False)

    @patch("kerkov.core.process.windows_kill._taskkill")
    def test_terminates_tree_with_psutil(self, mock_taskkill):
        child = MagicMock()
        parent = MagicMock()
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = parent
        parent.children.return_value = [child]
        with patch("kerkov.core.process.windows_kill.psutil", fake_psutil):
            windows_kill._graceful_kill(FAKE_PID)
        child.terminate.assert_called_once()
        parent.terminate.assert_called_once()
        mock_taskkill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
