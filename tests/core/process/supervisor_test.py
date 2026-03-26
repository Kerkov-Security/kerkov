import asyncio
import signal
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.process.adapter_interface import (
    ManagedRun,
    TerminationReason,
    WaitResult,
)
from src.core.process.supervisor import (
    ProcessSupervisor,
    build_cursor_position_response,
    get_process_supervisor,
    strip_dsr_requests,
    _is_timeout_reason,
)
import src.core.process.supervisor as supervisor_mod


def _create_mock_adapter(pid=1234, wait_result=None):
    """Create a mock adapter implementing the ProcessAdapter interface."""
    adapter = MagicMock()
    adapter.pid = pid
    adapter.stdin = MagicMock()
    adapter.on_stdout = MagicMock()
    adapter.on_stderr = MagicMock()
    adapter.wait = AsyncMock(return_value=wait_result or WaitResult(code=0))
    adapter.kill = MagicMock()
    adapter.dispose = MagicMock()
    return adapter


# ---------------------------------------------------------------------------
# strip_dsr_requests
# ---------------------------------------------------------------------------
class TestStripDsrRequests(unittest.TestCase):

    def test_no_dsr_returns_original(self):
        text = "hello world"
        cleaned, count = strip_dsr_requests(text)
        assert cleaned == "hello world"
        assert count == 0

    def test_strips_standard_dsr(self):
        text = "before\x1b[6nafter"
        cleaned, count = strip_dsr_requests(text)
        assert cleaned == "beforeafter"
        assert count == 1

    def test_strips_private_dsr(self):
        text = "before\x1b[?6nafter"
        cleaned, count = strip_dsr_requests(text)
        assert cleaned == "beforeafter"
        assert count == 1

    def test_strips_multiple_dsr(self):
        text = "\x1b[6nhello\x1b[?6nworld\x1b[6n"
        cleaned, count = strip_dsr_requests(text)
        assert cleaned == "helloworld"
        assert count == 3

    def test_empty_string(self):
        cleaned, count = strip_dsr_requests("")
        assert cleaned == ""
        assert count == 0

    def test_only_dsr(self):
        cleaned, count = strip_dsr_requests("\x1b[6n")
        assert cleaned == ""
        assert count == 1


# ---------------------------------------------------------------------------
# build_cursor_position_response
# ---------------------------------------------------------------------------
class TestBuildCursorPositionResponse(unittest.TestCase):

    def test_default_row_col(self):
        assert build_cursor_position_response() == "\x1b[1;1R"

    def test_custom_row_col(self):
        assert build_cursor_position_response(10, 25) == "\x1b[10;25R"

    def test_row_only(self):
        assert build_cursor_position_response(row=5) == "\x1b[5;1R"


# ---------------------------------------------------------------------------
# _is_timeout_reason
# ---------------------------------------------------------------------------
class TestIsTimeoutReason(unittest.TestCase):

    def test_overall_timeout(self):
        assert _is_timeout_reason(TerminationReason.OVERALL_TIMEOUT) is True

    def test_no_output_timeout(self):
        assert _is_timeout_reason(TerminationReason.NO_OUTPUT_TIMEOUT) is True

    def test_exit_is_not_timeout(self):
        assert _is_timeout_reason(TerminationReason.EXIT) is False

    def test_signal_is_not_timeout(self):
        assert _is_timeout_reason(TerminationReason.SIGNAL) is False

    def test_manual_cancel_is_not_timeout(self):
        assert _is_timeout_reason(TerminationReason.MANUAL_CANCEL) is False

    def test_spawn_error_is_not_timeout(self):
        assert _is_timeout_reason(TerminationReason.SPAWN_ERROR) is False


# ---------------------------------------------------------------------------
# ProcessSupervisor — spawn basics
# ---------------------------------------------------------------------------
class TestSupervisorSpawnBasics(unittest.TestCase):

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_spawn_pty_mode_returns_managed_run(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty", run_id="test01")
        )

        assert isinstance(run, ManagedRun)
        assert run.run_id == "test01"
        assert run.pid == 1234
        assert run.stdin is adapter.stdin
        mock_pty_cls.create.assert_awaited_once()

    @patch("src.core.process.supervisor.ChildAdapter")
    def test_spawn_child_mode_skips_pty(self, mock_child_cls):
        adapter = _create_mock_adapter()
        mock_child_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="child", run_id="test02")
        )

        assert run.run_id == "test02"
        mock_child_cls.create.assert_awaited_once_with(
            "echo hi", cwd=None, env=None, stdin_mode="pipe-open"
        )

    @patch("src.core.process.supervisor.ChildAdapter")
    @patch("src.core.process.supervisor.PtyAdapter")
    def test_pty_failure_falls_back_to_child(self, mock_pty_cls, mock_child_cls):
        mock_pty_cls.create = AsyncMock(side_effect=OSError("no pty"))
        adapter = _create_mock_adapter()
        mock_child_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty", run_id="test03")
        )

        mock_pty_cls.create.assert_awaited_once()
        mock_child_cls.create.assert_awaited_once()
        assert run.pid == 1234

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_spawn_generates_run_id_when_empty(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty")
        )

        assert len(run.run_id) == 8

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_spawn_generates_run_id_when_whitespace(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty", run_id="   ")
        )

        assert run.run_id.strip() != ""
        assert run.run_id != "   "

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_spawn_registers_in_active(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty", run_id="active01")
        )

        assert supervisor.get_active("active01") is run


# ---------------------------------------------------------------------------
# ProcessSupervisor — output callbacks
# ---------------------------------------------------------------------------
class TestSupervisorOutputCallbacks(unittest.TestCase):

    @patch("src.core.process.supervisor.ChildAdapter")
    def test_stdout_callback_invoked_in_child_mode(self, mock_child_cls):
        adapter = _create_mock_adapter()
        mock_child_cls.create = AsyncMock(return_value=adapter)

        received = []
        supervisor = ProcessSupervisor()
        asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(
                command="echo hi", mode="child", run_id="cb01",
                on_stdout=lambda chunk: received.append(chunk),
            )
        )

        # Get the registered stdout callback and invoke it
        stdout_cb = adapter.on_stdout.call_args[0][0]
        stdout_cb("hello")

        assert received == ["hello"]

    @patch("src.core.process.supervisor.ChildAdapter")
    def test_stderr_callback_invoked(self, mock_child_cls):
        adapter = _create_mock_adapter()
        mock_child_cls.create = AsyncMock(return_value=adapter)

        received = []
        supervisor = ProcessSupervisor()
        asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(
                command="echo hi", mode="child", run_id="cb02",
                on_stderr=lambda chunk: received.append(chunk),
            )
        )

        stderr_cb = adapter.on_stderr.call_args[0][0]
        stderr_cb("error msg")

        assert received == ["error msg"]

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_pty_stdout_strips_dsr_and_responds(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        received = []
        supervisor = ProcessSupervisor()
        asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(
                command="echo hi", mode="pty", run_id="dsr01",
                on_stdout=lambda chunk: received.append(chunk),
            )
        )

        stdout_cb = adapter.on_stdout.call_args[0][0]
        # Chunk with a DSR request embedded
        stdout_cb("output\x1b[6nmore")

        # DSR stripped from user callback
        assert received == ["outputmore"]
        # Auto-responded with CPR
        adapter.stdin.write.assert_called_once_with("\x1b[1;1R")

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_pty_stdout_no_callback_when_only_dsr(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        received = []
        supervisor = ProcessSupervisor()
        asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(
                command="echo hi", mode="pty", run_id="dsr02",
                on_stdout=lambda chunk: received.append(chunk),
            )
        )

        stdout_cb = adapter.on_stdout.call_args[0][0]
        stdout_cb("\x1b[6n")

        # Empty string after stripping — callback not invoked
        assert received == []


# ---------------------------------------------------------------------------
# ProcessSupervisor — wait / lifecycle
# ---------------------------------------------------------------------------
class TestSupervisorWaitLifecycle(unittest.TestCase):

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_wait_returns_run_exit_on_normal_exit(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=0))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="echo hi", mode="pty", run_id="w01")
            return await run.wait()

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert result.reason == TerminationReason.EXIT
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.no_output_timed_out is False
        adapter.dispose.assert_called_once()

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_wait_detects_signal_exit(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=None, signal=9))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="sleep 999", mode="pty", run_id="w02")
            return await run.wait()

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert result.reason == TerminationReason.SIGNAL
        assert result.exit_signal == 9

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_wait_removes_from_active(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=0))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="echo hi", mode="pty", run_id="w03")
            assert supervisor.get_active("w03") is run
            await run.wait()
            return supervisor.get_active("w03")

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result is None

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_wait_collects_stdout_and_stderr(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=0))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(
                command="echo hi", mode="pty", run_id="w04",
            )
            # Simulate output before wait
            stdout_cb = adapter.on_stdout.call_args[0][0]
            stderr_cb = adapter.on_stderr.call_args[0][0]
            stdout_cb("line1\n")
            stdout_cb("line2\n")
            stderr_cb("warn\n")
            return await run.wait()

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert result.stdout == "line1\nline2\n"
        assert result.stderr == "warn\n"

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_wait_exception_disposes_and_removes(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        adapter.wait = AsyncMock(side_effect=RuntimeError("boom"))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="bad", mode="pty", run_id="w05")
            with self.assertRaises(RuntimeError):
                await run.wait()

        asyncio.get_event_loop().run_until_complete(run_test())

        adapter.dispose.assert_called_once()
        assert supervisor.get_active("w05") is None


# ---------------------------------------------------------------------------
# ProcessSupervisor — cancel
# ---------------------------------------------------------------------------
class TestSupervisorCancel(unittest.TestCase):

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_cancel_sends_sigkill(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="sleep 999", mode="pty", run_id="c01")
        )

        supervisor.cancel("c01")

        adapter.kill.assert_called_once_with(signal.SIGKILL)

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_cancel_with_custom_reason(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=None, signal=9))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="sleep 999", mode="pty", run_id="c02")
            supervisor.cancel("c02", TerminationReason.OVERALL_TIMEOUT)
            return await run.wait()

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert result.reason == TerminationReason.OVERALL_TIMEOUT
        assert result.timed_out is True

    def test_cancel_unknown_run_id_is_noop(self):
        supervisor = ProcessSupervisor()
        supervisor.cancel("nonexistent")  # Should not raise

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_cancel_after_settled_is_noop(self, mock_pty_cls):
        adapter = _create_mock_adapter(wait_result=WaitResult(code=0))
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        async def run_test():
            run = await supervisor.spawn(command="echo hi", mode="pty", run_id="c03")
            await run.wait()  # Settles the run
            # Cancel after settlement — kill should not be called again
            adapter.kill.reset_mock()
            run.cancel(TerminationReason.MANUAL_CANCEL)

        asyncio.get_event_loop().run_until_complete(run_test())

        adapter.kill.assert_not_called()


# ---------------------------------------------------------------------------
# ProcessSupervisor — timeouts
# ---------------------------------------------------------------------------
class TestSupervisorTimeouts(unittest.TestCase):

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_overall_timeout_schedules_call_later(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        loop = asyncio.new_event_loop()
        scheduled = []
        original_call_later = loop.call_later

        def tracking_call_later(delay, callback, *args, **kwargs):
            scheduled.append(delay)
            return original_call_later(delay, callback, *args, **kwargs)

        loop.call_later = tracking_call_later

        try:
            loop.run_until_complete(
                supervisor.spawn(
                    command="sleep 999", mode="pty", run_id="t01",
                    timeout_ms=5000,
                )
            )
            # 5000ms = 5.0 seconds should be in scheduled delays
            assert 5.0 in scheduled
        finally:
            loop.close()

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_no_output_timeout_schedules_call_later(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        loop = asyncio.new_event_loop()
        scheduled = []
        original_call_later = loop.call_later

        def tracking_call_later(delay, callback, *args, **kwargs):
            scheduled.append(delay)
            return original_call_later(delay, callback, *args, **kwargs)

        loop.call_later = tracking_call_later

        try:
            loop.run_until_complete(
                supervisor.spawn(
                    command="sleep 999", mode="pty", run_id="t02",
                    no_output_timeout_ms=3000,
                )
            )
            assert 3.0 in scheduled
        finally:
            loop.close()

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_no_timers_when_no_timeouts(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        loop = asyncio.new_event_loop()
        scheduled = []
        original_call_later = loop.call_later

        def tracking_call_later(delay, callback, *args, **kwargs):
            scheduled.append(delay)
            return original_call_later(delay, callback, *args, **kwargs)

        loop.call_later = tracking_call_later

        try:
            loop.run_until_complete(
                supervisor.spawn(
                    command="echo hi", mode="pty", run_id="t03",
                    timeout_ms=None, no_output_timeout_ms=None,
                )
            )
            assert scheduled == []
        finally:
            loop.close()

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_zero_timeout_does_not_schedule(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()

        loop = asyncio.new_event_loop()
        scheduled = []
        original_call_later = loop.call_later

        def tracking_call_later(delay, callback, *args, **kwargs):
            scheduled.append(delay)
            return original_call_later(delay, callback, *args, **kwargs)

        loop.call_later = tracking_call_later

        try:
            loop.run_until_complete(
                supervisor.spawn(
                    command="echo hi", mode="pty", run_id="t04",
                    timeout_ms=0, no_output_timeout_ms=0,
                )
            )
            assert scheduled == []
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# ProcessSupervisor — get_active
# ---------------------------------------------------------------------------
class TestSupervisorGetActive(unittest.TestCase):

    def test_get_active_unknown_returns_none(self):
        supervisor = ProcessSupervisor()
        assert supervisor.get_active("nonexistent") is None

    @patch("src.core.process.supervisor.PtyAdapter")
    def test_get_active_returns_managed_run(self, mock_pty_cls):
        adapter = _create_mock_adapter()
        mock_pty_cls.create = AsyncMock(return_value=adapter)

        supervisor = ProcessSupervisor()
        run = asyncio.get_event_loop().run_until_complete(
            supervisor.spawn(command="echo hi", mode="pty", run_id="ga01")
        )

        assert supervisor.get_active("ga01") is run


# ---------------------------------------------------------------------------
# get_process_supervisor singleton
# ---------------------------------------------------------------------------
class TestGetProcessSupervisor(unittest.TestCase):

    def tearDown(self):
        supervisor_mod._supervisor = None

    def test_returns_supervisor_instance(self):
        sup = get_process_supervisor()
        assert isinstance(sup, ProcessSupervisor)

    def test_returns_same_instance(self):
        s1 = get_process_supervisor()
        s2 = get_process_supervisor()
        assert s1 is s2

    def test_thread_safe_singleton(self):
        supervisor_mod._supervisor = None

        results = []
        barrier = threading.Barrier(4)

        def get_sup():
            barrier.wait()
            results.append(get_process_supervisor())

        threads = [threading.Thread(target=get_sup) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 4
        assert all(r is results[0] for r in results)
