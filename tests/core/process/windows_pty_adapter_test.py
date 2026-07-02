import sys

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows PTY adapter (pywinpty/ConPTY)",
        allow_module_level=True,
    )

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from winpty import PtyProcess

from src.core.process.windows_pty_adapter import (
    WindowsPtyAdapter,
    WindowsPtyAdapterStdin,
)
from src.core.process.adapter_interface import KillMode


def _create_mock_pty(pid=1234, read_return_value="", exitstatus=0) -> MagicMock:
    mock = MagicMock(spec=PtyProcess)
    mock.pid = pid
    mock.read.return_value = read_return_value
    mock.exitstatus = exitstatus
    return mock


class WindowsPtyAdapterStdinTest(unittest.TestCase):

    def test_write_passes_str_through(self):
        pty = _create_mock_pty()
        stdin = WindowsPtyAdapterStdin(pty)

        stdin.write("hello world")

        pty.write.assert_called_once_with("hello world")

    def test_write_suppresses_os_error(self):
        pty = _create_mock_pty()
        pty.write.side_effect = OSError("closed")
        stdin = WindowsPtyAdapterStdin(pty)

        stdin.write("hello")  # should not raise

    def test_close_sends_eof(self):
        pty = _create_mock_pty()
        stdin = WindowsPtyAdapterStdin(pty)

        stdin.close()

        pty.sendeof.assert_called_once()

    def test_close_is_idempotent(self):
        pty = _create_mock_pty()
        stdin = WindowsPtyAdapterStdin(pty)

        stdin.close()
        pty.sendeof.reset_mock()
        stdin.close()

        pty.sendeof.assert_not_called()

    def test_write_after_close_is_noop(self):
        pty = _create_mock_pty()
        stdin = WindowsPtyAdapterStdin(pty)

        stdin.close()
        pty.write.reset_mock()
        stdin.write("foo")

        pty.write.assert_not_called()


class WindowsPtyAdapterTest(unittest.TestCase):

    @patch("src.core.process.windows_pty_adapter.PtyProcess")
    @patch("src.core.process.windows_pty_adapter.PTY")
    def test_create_uses_verbatim_cmd_cmdline(self, mock_pty_cls, mock_ptyprocess_cls):
        mock_pty = MagicMock()
        mock_pty_cls.return_value = mock_pty
        mock_ptyprocess_cls.return_value = _create_mock_pty()

        adapter = asyncio.run(WindowsPtyAdapter.create("echo hello"))
        try:
            cmdline = mock_pty.spawn.call_args.kwargs["cmdline"]
            assert '/d /s /c "echo hello"' in cmdline
        finally:
            adapter.dispose()

    @patch("src.core.process.windows_pty_adapter.PtyProcess")
    @patch("src.core.process.windows_pty_adapter.PTY")
    def test_create_passes_dimensions(self, mock_pty_cls, mock_ptyprocess_cls):
        mock_pty_cls.return_value = MagicMock()
        mock_ptyprocess_cls.return_value = _create_mock_pty()

        adapter = asyncio.run(WindowsPtyAdapter.create("echo hi", cols=200, rows=50))
        try:
            mock_pty_cls.assert_called_once_with(200, 50)
        finally:
            adapter.dispose()

    def test_pid_returns_pty_pid(self):
        pty = _create_mock_pty(pid=42)
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=asyncio.new_event_loop())
        assert adapter.pid == 42

    def test_stdin_returns_adapter_stdin_instance(self):
        pty = _create_mock_pty()
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=MagicMock())
        assert isinstance(adapter.stdin, WindowsPtyAdapterStdin)

    def test_on_stdout_delivers_str_output(self):
        pty = _create_mock_pty()
        pty.read.side_effect = ["hello world", EOFError]
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=MagicMock())

        received = []
        adapter.on_stdout(received.append)
        adapter._reader_thread.join(timeout=1)

        assert received == ["hello world"]

    def test_wait_returns_exit_code_without_signal(self):
        pty = _create_mock_pty(exitstatus=42)
        loop = asyncio.new_event_loop()
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=loop)

        adapter._start_exit_watcher()
        result = loop.run_until_complete(adapter.wait())

        assert result.code == 42
        assert result.signal is None
        adapter.dispose()
        loop.close()

    @patch("src.core.process.windows_pty_adapter.kill_process_tree")
    def test_kill_force_uses_tree_kill_and_terminate(self, mock_tree_kill):
        pty = _create_mock_pty(pid=99)
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.kill(KillMode.FORCE)

        mock_tree_kill.assert_called_once_with(99)
        pty.terminate.assert_called_with(force=True)

    def test_kill_graceful_calls_terminate(self):
        pty = _create_mock_pty(pid=99)
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=MagicMock())

        adapter.kill(KillMode.GRACEFUL)

        pty.terminate.assert_called_once_with(force=False)

    def test_dispose_settles_pending_wait(self):
        pty = _create_mock_pty()
        loop = asyncio.new_event_loop()
        adapter = WindowsPtyAdapter(pty_proc=pty, loop=loop)

        adapter.dispose()
        result = loop.run_until_complete(adapter.wait())

        assert result.code is None
        assert result.signal is None
        loop.close()


if __name__ == "__main__":
    unittest.main()
