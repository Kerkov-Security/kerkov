import asyncio
import signal
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from src.core.process.child_adapter import ChildAdapterStdin, ChildAdapter
from src.core.process.adapter_interface import AdapterStdin, ProcessAdapter, WaitResult


def _create_mock_proc(
    pid=1234,
    returncode=0,
    stdin=None,
    stdout=None,
    stderr=None,
) -> MagicMock:
    mock = MagicMock(spec=asyncio.subprocess.Process)
    mock.pid = pid
    mock.stdin = stdin
    mock.stdout = stdout
    mock.stderr = stderr
    mock.wait = AsyncMock(return_value=returncode)
    mock.send_signal = MagicMock()
    return mock


def _create_mock_writer() -> MagicMock:
    mock = MagicMock(spec=asyncio.StreamWriter)
    mock.write = MagicMock()
    mock.close = MagicMock()
    mock.drain = AsyncMock()
    return mock


def _create_mock_reader(chunks: list[bytes]) -> MagicMock:
    it = iter(chunks)
    mock = MagicMock()
    mock.read = AsyncMock(side_effect=it)
    return mock


# ---------------------------------------------------------------------------
# ChildAdapterStdin
# ---------------------------------------------------------------------------

class ChildAdapterStdinTest(unittest.TestCase):

    def test_inherits_adapter_stdin(self):
        assert issubclass(ChildAdapterStdin, AdapterStdin)

    def test_write_encodes_and_sends_utf8(self):
        writer = _create_mock_writer()
        stdin = ChildAdapterStdin(writer)

        stdin.write("hello world")

        writer.write.assert_called_once_with(b"hello world")

    def test_write_suppresses_os_error(self):
        writer = _create_mock_writer()
        writer.write.side_effect = OSError("broken pipe")
        stdin = ChildAdapterStdin(writer)

        stdin.write("hello")

    def test_write_suppresses_connection_reset_error(self):
        writer = _create_mock_writer()
        writer.write.side_effect = ConnectionResetError
        stdin = ChildAdapterStdin(writer)

        stdin.write("hello")

    def test_close_closes_writer(self):
        writer = _create_mock_writer()
        stdin = ChildAdapterStdin(writer)

        stdin.close()

        writer.close.assert_called_once()

    def test_close_is_idempotent(self):
        writer = _create_mock_writer()
        stdin = ChildAdapterStdin(writer)

        stdin.close()
        writer.close.reset_mock()
        stdin.close()

        writer.close.assert_not_called()

    def test_write_after_close_is_noop(self):
        writer = _create_mock_writer()
        stdin = ChildAdapterStdin(writer)

        stdin.close()
        writer.write.reset_mock()
        stdin.write("foo")

        writer.write.assert_not_called()

    def test_close_suppresses_os_error(self):
        writer = _create_mock_writer()
        writer.close.side_effect = OSError("fd closed")
        stdin = ChildAdapterStdin(writer)

        stdin.close()

    def test_close_suppresses_connection_reset_error(self):
        writer = _create_mock_writer()
        writer.close.side_effect = ConnectionResetError
        stdin = ChildAdapterStdin(writer)

        stdin.close()


# ---------------------------------------------------------------------------
# ChildAdapter
# ---------------------------------------------------------------------------

class ChildAdapterTest(unittest.TestCase):

    # --- ABC contract ---

    def test_inherits_process_adapter(self):
        assert issubclass(ChildAdapter, ProcessAdapter)

    # --- Construction ---

    def test_wraps_stdin_when_present(self):
        writer = _create_mock_writer()
        proc = _create_mock_proc(stdin=writer)
        adapter = ChildAdapter(proc)

        assert isinstance(adapter.stdin, ChildAdapterStdin)

    def test_stdin_is_none_when_proc_has_no_stdin(self):
        proc = _create_mock_proc(stdin=None)
        adapter = ChildAdapter(proc)

        assert adapter.stdin is None

    # --- Properties ---

    def test_pid_returns_proc_pid(self):
        proc = _create_mock_proc(pid=42)
        adapter = ChildAdapter(proc)

        assert adapter.pid == 42

    # --- create() factory ---

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_spawns_bash_shell_wrapper(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("echo hello")
        )

        args = mock_exec.call_args[0]
        assert args == ("bash", "-c", "echo hello")
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_passes_cwd_and_env(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)
        test_env = {"FOO": "bar"}

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("ls", cwd="/tmp", env=test_env)
        )

        kwargs = mock_exec.call_args[1]
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["env"] == test_env
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_uses_start_new_session(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("echo hi")
        )

        kwargs = mock_exec.call_args[1]
        assert kwargs["start_new_session"] is True
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_closes_stdin_by_default(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("echo hi")
        )

        writer.close.assert_called_once()
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_leaves_stdin_open_when_pipe_open(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("cat", stdin_mode="pipe-open")
        )

        writer.close.assert_not_called()
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_writes_input_data(self, mock_exec):
        writer = _create_mock_writer()
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("cat", input_data="hello\n")
        )

        writer.write.assert_any_call(b"hello\n")
        adapter.dispose()

    @patch("src.core.process.child_adapter.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    def test_create_suppresses_write_error_on_input_data(self, mock_exec):
        writer = _create_mock_writer()
        writer.write.side_effect = OSError("broken")
        mock_exec.return_value = _create_mock_proc(stdin=writer)

        adapter = asyncio.get_event_loop().run_until_complete(
            ChildAdapter.create("cat", input_data="hello\n")
        )
        adapter.dispose()

    # --- on_stdout / on_stderr ---

    def test_on_stdout_noop_when_no_stdout_stream(self):
        proc = _create_mock_proc(stdout=None)
        adapter = ChildAdapter(proc)

        adapter.on_stdout(lambda _: None)

        assert adapter._stdout_task is None

    def test_on_stderr_noop_when_no_stderr_stream(self):
        proc = _create_mock_proc(stderr=None)
        adapter = ChildAdapter(proc)

        adapter.on_stderr(lambda _: None)

        assert adapter._stderr_task is None

    def test_on_stdout_creates_task(self):
        reader = _create_mock_reader([b"hello", b""])
        proc = _create_mock_proc(stdout=reader)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        received = []

        async def run():
            adapter.on_stdout(received.append)
            await adapter._stdout_task

        loop.run_until_complete(run())
        loop.close()

        assert received == ["hello"]

    def test_on_stderr_creates_task(self):
        reader = _create_mock_reader([b"error msg", b""])
        proc = _create_mock_proc(stderr=reader)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        received = []

        async def run():
            adapter.on_stderr(received.append)
            await adapter._stderr_task

        loop.run_until_complete(run())
        loop.close()

        assert received == ["error msg"]

    def test_on_stdout_handles_os_error(self):
        reader = MagicMock()
        reader.read = AsyncMock(side_effect=OSError("fd closed"))
        proc = _create_mock_proc(stdout=reader)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()

        async def run():
            adapter.on_stdout(lambda _: None)
            await adapter._stdout_task

        loop.run_until_complete(run())
        loop.close()

    def test_on_stdout_delivers_multiple_chunks(self):
        reader = _create_mock_reader([b"chunk1", b"chunk2", b""])
        proc = _create_mock_proc(stdout=reader)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        received = []

        async def run():
            adapter.on_stdout(received.append)
            await adapter._stdout_task

        loop.run_until_complete(run())
        loop.close()

        assert received == ["chunk1", "chunk2"]

    # --- wait() ---

    def test_wait_returns_exit_code(self):
        proc = _create_mock_proc(returncode=42)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(adapter.wait())
        loop.close()

        assert result.code == 42
        assert result.signal is None

    def test_wait_returns_signal_for_negative_returncode(self):
        proc = _create_mock_proc(returncode=-9)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(adapter.wait())
        loop.close()

        assert result.code is None
        assert result.signal == 9

    def test_wait_returns_zero_exit_code(self):
        proc = _create_mock_proc(returncode=0)
        adapter = ChildAdapter(proc)

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(adapter.wait())
        loop.close()

        assert result.code == 0
        assert result.signal is None

    # --- kill() ---

    @patch("src.core.process.child_adapter.kill_process_tree")
    def test_kill_sigkill_uses_tree_kill(self, mock_tree_kill):
        proc = _create_mock_proc(pid=99)
        adapter = ChildAdapter(proc)

        adapter.kill(signal.SIGKILL)

        mock_tree_kill.assert_called_once_with(99)

    def test_kill_other_signal_uses_send_signal(self):
        proc = _create_mock_proc(pid=99)
        adapter = ChildAdapter(proc)

        adapter.kill(signal.SIGTERM)

        proc.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_kill_sigkill_with_none_pid_falls_back_to_send_signal(self):
        proc = _create_mock_proc(pid=None)
        adapter = ChildAdapter(proc)

        with patch("src.core.process.child_adapter.kill_process_tree") as mock_tree_kill:
            adapter.kill(signal.SIGKILL)
            mock_tree_kill.assert_not_called()

        proc.send_signal.assert_called_once_with(signal.SIGKILL)

    def test_kill_suppresses_os_error(self):
        proc = _create_mock_proc(pid=99)
        proc.send_signal.side_effect = OSError("no such process")
        adapter = ChildAdapter(proc)

        adapter.kill(signal.SIGTERM)

    def test_kill_suppresses_process_lookup_error(self):
        proc = _create_mock_proc(pid=99)
        proc.send_signal.side_effect = ProcessLookupError
        adapter = ChildAdapter(proc)

        adapter.kill(signal.SIGTERM)

    # --- dispose() ---

    def test_dispose_cancels_stdout_task(self):
        proc = _create_mock_proc()
        adapter = ChildAdapter(proc)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        adapter._stdout_task = mock_task

        adapter.dispose()

        mock_task.cancel.assert_called_once()
        assert adapter._stdout_task is None

    def test_dispose_cancels_stderr_task(self):
        proc = _create_mock_proc()
        adapter = ChildAdapter(proc)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        adapter._stderr_task = mock_task

        adapter.dispose()

        mock_task.cancel.assert_called_once()
        assert adapter._stderr_task is None

    def test_dispose_skips_done_tasks(self):
        proc = _create_mock_proc()
        adapter = ChildAdapter(proc)
        mock_task = MagicMock()
        mock_task.done.return_value = True
        adapter._stdout_task = mock_task

        adapter.dispose()

        mock_task.cancel.assert_not_called()
        assert adapter._stdout_task is None

    def test_dispose_is_idempotent(self):
        proc = _create_mock_proc()
        adapter = ChildAdapter(proc)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        adapter._stdout_task = mock_task

        adapter.dispose()
        mock_task.cancel.reset_mock()
        adapter.dispose()

        mock_task.cancel.assert_not_called()

    def test_dispose_handles_no_tasks(self):
        proc = _create_mock_proc()
        adapter = ChildAdapter(proc)

        adapter.dispose()

        assert adapter._stdout_task is None
        assert adapter._stderr_task is None


if __name__ == "__main__":
    unittest.main()
