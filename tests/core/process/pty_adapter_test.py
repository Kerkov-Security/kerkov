import asyncio
import sys
import signal
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from ptyprocess import PtyProcess

from src.core.process.pty_adapter import PtyAdapterStdin, PtyAdapter
from src.core.process.adapter_interface import WaitResult

def _create_mock_pty(
    pid = 1234,
    read_return_value = b"",
    exitstatus = 0,
    signalstatus = None,
) -> MagicMock:
    mock = MagicMock(spec=PtyProcess)
    mock.pid = pid
    mock.read.return_value = read_return_value
    mock.exitstatus = exitstatus
    mock.signalstatus = signalstatus
    return mock

class PtyAdapterStdinTest(unittest.TestCase):
    """Unit tests for PtyAdapterStdin."""

    def test_write_encodes_and_sends_utf8(self):
        pty = _create_mock_pty()
        stdin = PtyAdapterStdin(pty)

        stdin.write("hello world")

        pty.write.assert_called_once_with(b"hello world")

    def test_write_suppresses_os_error(self):            
        pty = _create_mock_pty()
        pty.write.side_effect = OSError("fd closed")
        stdin = PtyAdapterStdin(pty)

        stdin.write("hello")  # should not raise

    def test_write_suppresses_eof_error(self):
      pty = _create_mock_pty()
      pty.write.side_effect = EOFError("eof")
      stdin = PtyAdapterStdin(pty)

      stdin.write("hello")  # should not raise

    def test_close_sends_ctrl_d(self):
        pty = _create_mock_pty()
        stdin = PtyAdapterStdin(pty)

        stdin.close()

        pty.write.assert_called_once_with(b"\x04")

    def test_close_is_idempotent(self):
        pty = _create_mock_pty()
        stdin = PtyAdapterStdin(pty)

        stdin.close()
        pty.write.reset_mock()
        stdin.close()

        pty.write.assert_not_called()

    def test_write_after_close_is_noop(self):
        pty = _create_mock_pty()
        stdin = PtyAdapterStdin(pty)

        stdin.close()
        pty.write.reset_mock()
        stdin.write("foo")

        pty.write.assert_not_called()

    def test_close_suppresses_eof_error(self):
        pty = _create_mock_pty()
        pty.write.side_effect = EOFError("eof")
        stdin = PtyAdapterStdin(pty)

        stdin.close()  # should not raise

class PtyAdapterTest(unittest.TestCase):
    """Unit tests for PtyAdapter (mocked PtyProcess)."""

    # --- Construction / create() ---

    @patch.object(PtyProcess, "spawn", return_value=_create_mock_pty())
    def test_create_spawns_with_bash_shell_wrapper(self, mock_spawn):
        adapter = asyncio.get_event_loop().run_until_complete(
            PtyAdapter.create("echo hello")
        )
        try:
            args, kwargs = mock_spawn.call_args
            self.assertEqual(args[0], ["bash", "-c", "echo hello"])
        finally:
            adapter.dispose()

    @patch.object(PtyProcess, "spawn", return_value=_create_mock_pty())
    def test_create_passes_cwd(self, mock_spawn):
        adapter = asyncio.get_event_loop().run_until_complete(
            PtyAdapter.create("ls", cwd="/tmp")
        )
        try:
            _, kwargs = mock_spawn.call_args
            self.assertEqual(kwargs["cwd"], "/tmp")
        finally:
            adapter.dispose()

    @patch.object(PtyProcess, "spawn", return_value=_create_mock_pty())
    def test_create_passes_env(self, mock_spawn):
        test_env = {"FOO": "bar", "BAZ": "qux"}
        adapter = asyncio.get_event_loop().run_until_complete(
            PtyAdapter.create("env", env=test_env)
        )
        try:
            _, kwargs = mock_spawn.call_args
            self.assertEqual(kwargs["env"], test_env)
        finally:
            adapter.dispose()

    @patch.object(PtyProcess, "spawn", return_value=_create_mock_pty())
    def test_create_passes_dimensions(self, mock_spawn):
        adapter = asyncio.get_event_loop().run_until_complete(
            PtyAdapter.create("echo hi", cols=200, rows=50)
        )
        try:
            _, kwargs = mock_spawn.call_args
            self.assertEqual(kwargs["dimensions"], (50, 200))
        finally:
            adapter.dispose()

    @patch.object(PtyProcess, "spawn", return_value=_create_mock_pty())
    def test_create_starts_exit_watcher(self, mock_spawn):
        pty = mock_spawn.return_value
        adapter = asyncio.get_event_loop().run_until_complete(
            PtyAdapter.create("echo hi")
        )
        try:
            # The exit watcher calls pty.wait() in a background thread
            pty.wait.assert_called_once()
        finally:
            adapter.dispose()

    # --- Properties ---

    def test_pid_returns_pty_pid(self):
        """pid property returns the PtyProcess pid."""
        pty = _create_mock_pty(pid=42)

        adapter = PtyAdapter(
            pty_proc=pty,
            loop=asyncio.new_event_loop(),
        )

        self.assertEqual(adapter.pid, 42)

    def test_pid_returns_none_on_exception(self):
        """pid property returns None if accessing pty.pid raises."""
        pty = _create_mock_pty()
        type(pty).pid = property(lambda self: (_ for _ in ()).throw(OSError("gone")))

        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        self.assertIsNone(adapter.pid)

    def test_stdin_returns_adapter_stdin_instance(self):
        """stdin property returns a PtyAdapterStdin wrapping the pty."""
        pty = _create_mock_pty()

        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        self.assertIsInstance(adapter.stdin, PtyAdapterStdin)

    # --- on_stdout ---

    def test_on_stdout_delivers_decoded_output(self):
        """on_stdout callback receives decoded string data from PTY reads."""
        pty = _create_mock_pty()
        pty.read.side_effect = [b"hello world", EOFError]
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        received = []
        adapter.on_stdout(received.append)
        adapter._reader_thread.join(timeout=1)

        self.assertEqual(received, ["hello world"])

    def test_on_stdout_handles_eof(self):
        """Reader thread exits cleanly when PTY read raises EOFError."""
        pty = _create_mock_pty()
        pty.read.side_effect = EOFError
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.on_stdout(lambda _: None)
        adapter._reader_thread.join(timeout=1)

        self.assertFalse(adapter._reader_thread.is_alive())

    def test_on_stdout_handles_os_error(self):
        """Reader thread exits cleanly when PTY read raises OSError."""
        pty = _create_mock_pty()
        pty.read.side_effect = OSError("fd closed")
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.on_stdout(lambda _: None)
        adapter._reader_thread.join(timeout=1)

        self.assertFalse(adapter._reader_thread.is_alive())

    def test_on_stdout_handles_empty_read(self):
        """Reader thread exits when PTY read returns empty data."""
        pty = _create_mock_pty()
        pty.read.return_value = b""
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.on_stdout(lambda _: None)
        adapter._reader_thread.join(timeout=1)

        self.assertFalse(adapter._reader_thread.is_alive())

    def test_on_stdout_only_registers_once(self):
        """Calling on_stdout a second time is a no-op."""
        pty = _create_mock_pty()
        pty.read.side_effect = EOFError
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.on_stdout(lambda _: None)
        first_thread = adapter._reader_thread
        adapter.on_stdout(lambda _: None)

        self.assertIs(adapter._reader_thread, first_thread)

    # --- wait() ---

    def test_wait_returns_exit_code(self):
        pty = _create_mock_pty(exitstatus=42, signalstatus=None)
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter._start_exit_watcher()
        result = loop.run_until_complete(adapter.wait())

        self.assertEqual(result.code, 42)
        self.assertIsNone(result.signal)
        adapter.dispose()
        loop.close()

    def test_wait_returns_signal(self):
        pty = _create_mock_pty(exitstatus=None, signalstatus=9)
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter._start_exit_watcher()
        result = loop.run_until_complete(adapter.wait())

        self.assertIsNone(result.code)
        self.assertEqual(result.signal, 9)
        adapter.dispose()
        loop.close()

    def test_wait_returns_cached_result(self):
        pty = _create_mock_pty(exitstatus=0, signalstatus=None)
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter._settle_wait(WaitResult(code=7, signal=None))
        result = loop.run_until_complete(adapter.wait())

        self.assertEqual(result.code, 7)
        adapter.dispose()
        loop.close()

    def test_wait_returns_none_when_pty_wait_raises(self):
        pty = _create_mock_pty()
        pty.wait.side_effect = RuntimeError("boom")
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter._start_exit_watcher()
        result = loop.run_until_complete(adapter.wait())

        self.assertIsNone(result.code)
        self.assertIsNone(result.signal)
        adapter.dispose()
        loop.close()

    # --- kill() ---

    @patch("src.core.process.pty_adapter.kill_process_tree")
    def test_kill_sigkill_uses_tree_kill(self, mock_tree_kill):
        pty = _create_mock_pty(pid=99)
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.kill(signal.SIGKILL)

        mock_tree_kill.assert_called_once_with(99)

    @patch("src.core.process.pty_adapter.os.kill")
    def test_kill_other_signal_uses_os_kill(self, mock_os_kill):
        pty = _create_mock_pty(pid=99)
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.kill(signal.SIGTERM)

        mock_os_kill.assert_called_once_with(99, signal.SIGTERM)

    def test_kill_sigkill_schedules_force_kill_fallback(self):
        pty = _create_mock_pty(pid=99)
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        with patch.object(adapter, "_schedule_force_kill_fallback") as mock_schedule:
            adapter.kill(signal.SIGKILL)
            mock_schedule.assert_called_once()

    def test_kill_non_sigkill_does_not_schedule_fallback(self):
        pty = _create_mock_pty(pid=99)
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        with patch.object(adapter, "_schedule_force_kill_fallback") as mock_schedule:
            adapter.kill(signal.SIGTERM)
            mock_schedule.assert_not_called()

    @patch("src.core.process.pty_adapter.os.kill", side_effect=OSError("no such process"))
    def test_kill_suppresses_os_error(self, _):
        pty = _create_mock_pty(pid=99)
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.kill(signal.SIGTERM)  # should not raise

    # --- dispose() ---

    def test_dispose_sets_reader_stop_event(self):
        pty = _create_mock_pty()
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.dispose()

        self.assertTrue(adapter._reader_stop.is_set())

    def test_dispose_cancels_force_kill_timer(self):
        pty = _create_mock_pty()
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())
        mock_timer = MagicMock()
        adapter._force_kill_timer = mock_timer

        adapter.dispose()

        mock_timer.cancel.assert_called_once()
        self.assertIsNone(adapter._force_kill_timer)

    def test_dispose_settles_pending_wait(self):
        pty = _create_mock_pty()
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter.dispose()
        result = loop.run_until_complete(adapter.wait())

        self.assertIsNone(result.code)
        self.assertIsNone(result.signal)
        loop.close()

    def test_dispose_is_idempotent(self):
        pty = _create_mock_pty()
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        with patch.object(adapter, "_settle_wait") as mock_settle:
            adapter.dispose()
            adapter.dispose()

            mock_settle.assert_called_once()

    # --- _settle_wait ---

    def test_settle_wait_is_idempotent(self):
        pty = _create_mock_pty()
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter._settle_wait(WaitResult(code=1, signal=None))
        adapter._settle_wait(WaitResult(code=2, signal=None))

        self.assertEqual(adapter._wait_result.code, 1)

    def test_settle_wait_signals_asyncio_event(self):
        pty = _create_mock_pty()
        mock_loop = MagicMock()
        adapter = PtyAdapter(pty_proc=pty, loop=mock_loop)

        adapter._settle_wait(WaitResult(code=0, signal=None))

        mock_loop.call_soon_threadsafe.assert_called_once_with(adapter._wait_event.set)

    def test_settle_wait_falls_back_when_loop_closed(self):
        pty = _create_mock_pty()
        mock_loop = MagicMock()
        mock_loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        adapter = PtyAdapter(pty_proc=pty, loop=mock_loop)

        adapter._settle_wait(WaitResult(code=0, signal=None))

        self.assertTrue(adapter._wait_event.is_set())

    # --- Force-kill fallback ---

    def test_force_kill_timer_settles_wait_with_sigkill(self):
        pty = _create_mock_pty()
        loop = asyncio.new_event_loop()
        adapter = PtyAdapter(pty_proc=pty, loop=loop)

        adapter._schedule_force_kill_fallback()
        # Fire the timer callback directly instead of waiting
        adapter._force_kill_timer.function()

        self.assertEqual(adapter._wait_result.signal, signal.SIGKILL)
        self.assertIsNone(adapter._wait_result.code)
        adapter._cancel_force_kill_timer()
        loop.close()

    def test_schedule_force_kill_cancels_existing_timer(self):
        pty = _create_mock_pty()
        adapter = PtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter._schedule_force_kill_fallback()
        first_timer = adapter._force_kill_timer

        adapter._schedule_force_kill_fallback()

        self.assertTrue(first_timer.finished.is_set())
        self.assertIsNot(adapter._force_kill_timer, first_timer)
        adapter._cancel_force_kill_timer()

if __name__ == "__main__":
    unittest.main()
