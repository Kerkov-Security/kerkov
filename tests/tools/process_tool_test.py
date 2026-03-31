import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from src.core.process.session_registry import (
    FinishedSession,
    ProcessSession,
    SessionRegistry,
)
from src.tools.base_tool import ToolResult
from src.tools.process_tool import (
    DEFAULT_LOG_TAIL_LINES,
    MAX_POLL_WAIT_MS,
    ProcessTool,
    ProcessToolParams,
    _format_duration,
    _resolve_poll_wait_ms,
    _resolve_writable_stdin,
    _truncate,
)


def _create_session(
    session_id: str = "abcd1234",
    command: str = "echo hello",
    backgrounded: bool = True,
    **kwargs,
) -> ProcessSession:
    s = ProcessSession(
        id=session_id,
        command=command,
        started_at=time.time(),
        **kwargs,
    )
    s.backgrounded = backgrounded
    return s


def _create_finished_session(
    session_id: str = "abcd1234",
    command: str = "echo hello",
    status: str = "completed",
    exit_code: int = 0,
    exit_signal: int = None,
    tail: str = "output here",
    aggregated: str = "full output here",
    **kwargs,
) -> FinishedSession:
    return FinishedSession(
        id=session_id,
        command=command,
        started_at=time.time() - 10,
        ended_at=time.time(),
        status=status,
        exit_code=exit_code,
        exit_signal=exit_signal,
        tail=tail,
        aggregated=aggregated,
        **kwargs,
    )


def _create_mock_stdin():
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.close = MagicMock()
    return stdin


# ---------------------------------------------------------------------------
# Helper: _format_duration
# ---------------------------------------------------------------------------
class FormatDurationTest(unittest.TestCase):

    def test_seconds(self):
        assert _format_duration(45) == "45s"

    def test_minutes(self):
        assert _format_duration(120) == "2m"

    def test_hours(self):
        assert _format_duration(7200) == "2.0h"

    def test_zero(self):
        assert _format_duration(0) == "0s"

    def test_boundary_59s(self):
        assert _format_duration(59) == "59s"

    def test_boundary_60s(self):
        assert _format_duration(60) == "1m"

    def test_boundary_3599s(self):
        assert _format_duration(3599) == "60m"

    def test_boundary_3600s(self):
        assert _format_duration(3600) == "1.0h"


# ---------------------------------------------------------------------------
# Helper: _truncate
# ---------------------------------------------------------------------------
class TruncateTest(unittest.TestCase):

    def test_short_text(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_length(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncated(self):
        result = _truncate("hello world", 6)
        assert len(result) == 6
        assert result.endswith("…")

    def test_empty(self):
        assert _truncate("", 5) == ""


# ---------------------------------------------------------------------------
# Helper: _resolve_poll_wait_ms
# ---------------------------------------------------------------------------
class ResolvePollWaitMsTest(unittest.TestCase):

    def test_none_returns_zero(self):
        assert _resolve_poll_wait_ms(None) == 0

    def test_valid_value(self):
        assert _resolve_poll_wait_ms(5000) == 5000

    def test_zero(self):
        assert _resolve_poll_wait_ms(0) == 0

    def test_max_boundary(self):
        assert _resolve_poll_wait_ms(MAX_POLL_WAIT_MS) == MAX_POLL_WAIT_MS

    def test_exceeds_max_clamped(self):
        assert _resolve_poll_wait_ms(MAX_POLL_WAIT_MS + 1000) == MAX_POLL_WAIT_MS

    def test_negative_clamped_to_zero(self):
        assert _resolve_poll_wait_ms(-100) == 0


# ---------------------------------------------------------------------------
# Helper: _resolve_writable_stdin
# ---------------------------------------------------------------------------
class ResolveWritableStdinTest(unittest.TestCase):

    def test_none_session(self):
        result = _resolve_writable_stdin(None, "sid")
        assert result is not None
        assert result.success is False
        assert "No active session" in result.error

    def test_not_backgrounded(self):
        s = _create_session(backgrounded=False)
        result = _resolve_writable_stdin(s, s.id)
        assert result is not None
        assert result.success is False
        assert "not backgrounded" in result.error

    def test_no_stdin(self):
        s = _create_session()
        s.stdin = None
        result = _resolve_writable_stdin(s, s.id)
        assert result is not None
        assert result.success is False
        assert "not writable" in result.error

    def test_valid_session_returns_none(self):
        s = _create_session()
        s.stdin = _create_mock_stdin()
        result = _resolve_writable_stdin(s, s.id)
        assert result is None


# ---------------------------------------------------------------------------
# ProcessToolParams validation
# ---------------------------------------------------------------------------
class ProcessToolParamsValidationTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()

    def test_valid_list_action(self):
        params = ProcessToolParams(action="list")
        assert self.tool.validate_params(params) is None

    def test_valid_poll_action(self):
        params = ProcessToolParams(action="poll", session_id="abc")
        assert self.tool.validate_params(params) is None

    def test_unknown_action(self):
        params = ProcessToolParams(action="unknown", session_id="abc")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "Unknown action" in error

    def test_missing_session_id_for_non_list(self):
        params = ProcessToolParams(action="poll")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "session_id is required" in error

    def test_list_does_not_require_session_id(self):
        params = ProcessToolParams(action="list")
        assert self.tool.validate_params(params) is None


# ---------------------------------------------------------------------------
# ProcessTool — list action
# ---------------------------------------------------------------------------
class ProcessToolListTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_list_empty(self):
        params = ProcessToolParams(action="list")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "No running or recent sessions" in result.output
        assert result.metadata["running_count"] == 0
        assert result.metadata["finished_count"] == 0

    def test_list_running_session(self):
        s = _create_session(session_id="run01", command="sleep 999")
        s.pid = 1234
        self.tool.registry.add_session(s)
        self.tool.registry.mark_backgrounded(s)

        params = ProcessToolParams(action="list")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "run01" in result.output
        assert "running" in result.output
        assert "sleep 999" in result.output
        assert result.metadata["running_count"] == 1

    def test_list_finished_session(self):
        s = _create_session(session_id="fin01", command="echo done")
        self.tool.registry.add_session(s)
        self.tool.registry.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        params = ProcessToolParams(action="list")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "fin01" in result.output
        assert result.metadata["finished_count"] == 1


# ---------------------------------------------------------------------------
# ProcessTool — poll action
# ---------------------------------------------------------------------------
class ProcessToolPollTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_poll_no_session_found(self):
        params = ProcessToolParams(action="poll", session_id="nonexistent")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No session found" in result.error

    def test_poll_finished_session_with_exit_code(self):
        s = _create_session(session_id="pf01")
        s.aggregated = "output text"
        self.tool.registry.add_session(s)
        self.tool.registry.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        params = ProcessToolParams(action="poll", session_id="pf01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "code 0" in result.output

    def test_poll_finished_session_with_signal(self):
        s = _create_session(session_id="pf02")
        self.tool.registry.add_session(s)
        self.tool.registry.mark_exited(s, exit_code=None, exit_signal=9, status="killed")

        params = ProcessToolParams(action="poll", session_id="pf02")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "signal 9" in result.output

    def test_poll_not_backgrounded(self):
        s = _create_session(session_id="pnb01", backgrounded=False)
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="poll", session_id="pnb01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "not backgrounded" in result.error

    def test_poll_running_no_new_output(self):
        s = _create_session(session_id="pr01")
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="poll", session_id="pr01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "no new output" in result.output
        assert "still running" in result.output
        assert result.metadata["status"] == "running"

    def test_poll_running_with_output(self):
        s = _create_session(session_id="pr02")
        self.tool.registry.add_session(s)
        self.tool.registry.append_output(s, "stdout", "hello world\n")

        params = ProcessToolParams(action="poll", session_id="pr02")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "hello world" in result.output
        assert "still running" in result.output

    def test_poll_exited_session(self):
        s = _create_session(session_id="pe01")
        self.tool.registry.add_session(s)
        self.tool.registry.append_output(s, "stdout", "done\n")
        s.exited = True
        s.exit_code = 0

        params = ProcessToolParams(action="poll", session_id="pe01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "exited" in result.output.lower() or "code 0" in result.output
        assert result.metadata["status"] in ("completed", "failed")


# ---------------------------------------------------------------------------
# ProcessTool — log action
# ---------------------------------------------------------------------------
class ProcessToolLogTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_log_no_session(self):
        params = ProcessToolParams(action="log", session_id="nonexistent")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No session found" in result.error

    def test_log_not_backgrounded(self):
        s = _create_session(session_id="lnb01", backgrounded=False)
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="log", session_id="lnb01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "not backgrounded" in result.error

    def test_log_running_session_shows_output(self):
        s = _create_session(session_id="lr01")
        s.aggregated = "line1\nline2\nline3"
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="log", session_id="lr01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "line1" in result.output
        assert "line3" in result.output
        assert result.metadata["total_lines"] == 3

    def test_log_default_tail(self):
        lines = [f"line{i}" for i in range(DEFAULT_LOG_TAIL_LINES + 50)]
        s = _create_session(session_id="lt01")
        s.aggregated = "\n".join(lines)
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="log", session_id="lt01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "showing last" in result.output
        assert result.metadata["total_lines"] == DEFAULT_LOG_TAIL_LINES + 50

    def test_log_with_offset_and_limit(self):
        lines = [f"line{i}" for i in range(10)]
        s = _create_session(session_id="lo01")
        s.aggregated = "\n".join(lines)
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="log", session_id="lo01", offset=2, limit=3)
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "line2" in result.output
        assert "line4" in result.output
        assert "line5" not in result.output

    def test_log_empty_output(self):
        s = _create_session(session_id="le01")
        s.aggregated = ""
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="log", session_id="le01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "no output yet" in result.output

    def test_log_finished_session(self):
        s = _create_session(session_id="lf01")
        s.aggregated = "final output"
        self.tool.registry.add_session(s)
        self.tool.registry.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        params = ProcessToolParams(action="log", session_id="lf01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "final output" in result.output
        assert result.metadata["status"] == "completed"


# ---------------------------------------------------------------------------
# ProcessTool — write action
# ---------------------------------------------------------------------------
class ProcessToolWriteTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_write_no_session(self):
        params = ProcessToolParams(action="write", session_id="nonexistent", data="hi")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No active session" in result.error

    def test_write_sends_data(self):
        s = _create_session(session_id="w01")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="write", session_id="w01", data="hello\n")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        s.stdin.write.assert_called_once_with("hello\n")
        assert "6 bytes" in result.output

    def test_write_with_eof(self):
        s = _create_session(session_id="w02")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="write", session_id="w02", data="x", eof=True)
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        s.stdin.write.assert_called_once_with("x")
        s.stdin.close.assert_called_once()
        assert "stdin closed" in result.output

    def test_write_empty_data(self):
        s = _create_session(session_id="w03")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="write", session_id="w03")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        s.stdin.write.assert_called_once_with("")


# ---------------------------------------------------------------------------
# ProcessTool — send_keys action
# ---------------------------------------------------------------------------
class ProcessToolSendKeysTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_send_keys_no_session(self):
        params = ProcessToolParams(action="send_keys", session_id="nonexistent", keys=["C-c"])
        result = asyncio.run(self.tool.run(params))
        assert result.success is False

    def test_send_keys_ctrl_c(self):
        s = _create_session(session_id="sk01")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="send_keys", session_id="sk01", keys=["C-c"])
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        s.stdin.write.assert_called_once()
        written = s.stdin.write.call_args[0][0]
        assert written == "\x03"  # Ctrl+C

    def test_send_keys_hex_bytes(self):
        s = _create_session(session_id="sk02")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="send_keys", session_id="sk02", hex_bytes=["0x04"])
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        written = s.stdin.write.call_args[0][0]
        assert written == "\x04"  # Ctrl+D

    def test_send_keys_no_data(self):
        s = _create_session(session_id="sk03")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="send_keys", session_id="sk03")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No key data" in result.error

    def test_send_keys_with_warnings(self):
        s = _create_session(session_id="sk04")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(
            action="send_keys", session_id="sk04",
            hex_bytes=["0xZZ"],  # Invalid hex
            keys=["Up"],  # Valid key to produce some data
        )
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "Warnings" in result.output


# ---------------------------------------------------------------------------
# ProcessTool — paste action
# ---------------------------------------------------------------------------
class ProcessToolPasteTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_paste_no_session(self):
        params = ProcessToolParams(action="paste", session_id="nonexistent", text="hi")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False

    def test_paste_bracketed(self):
        s = _create_session(session_id="p01")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="paste", session_id="p01", text="import sys", bracketed=True)
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        written = s.stdin.write.call_args[0][0]
        assert "\x1b[200~" in written  # Bracketed paste start
        assert "import sys" in written
        assert "\x1b[201~" in written  # Bracketed paste end
        assert "10 chars" in result.output

    def test_paste_unbracketed(self):
        s = _create_session(session_id="p02")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="paste", session_id="p02", text="hello", bracketed=False)
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        written = s.stdin.write.call_args[0][0]
        assert written == "hello"

    def test_paste_empty_text(self):
        s = _create_session(session_id="p03")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="paste", session_id="p03")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No paste text" in result.error

    def test_paste_auto_bracketed_from_pty_stdin(self):
        from src.core.process.pty_adapter import PtyAdapterStdin

        s = _create_session(session_id="p04")
        mock_pty_stdin = MagicMock(spec=PtyAdapterStdin)
        mock_pty_stdin.bracketed_paste_enabled = True
        s.stdin = mock_pty_stdin
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="paste", session_id="p04", text="data")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        written = mock_pty_stdin.write.call_args[0][0]
        assert "\x1b[200~" in written
        assert "data" in written
        assert "\x1b[201~" in written

    def test_paste_auto_unbracketed_plain_stdin(self):
        s = _create_session(session_id="p05")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        # bracketed not specified, stdin is not PtyAdapterStdin → unbracketed
        params = ProcessToolParams(action="paste", session_id="p05", text="plain")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        written = s.stdin.write.call_args[0][0]
        assert written == "plain"


# ---------------------------------------------------------------------------
# ProcessTool — submit action
# ---------------------------------------------------------------------------
class ProcessToolSubmitTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_submit_no_session(self):
        params = ProcessToolParams(action="submit", session_id="nonexistent")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False

    def test_submit_sends_cr(self):
        s = _create_session(session_id="sub01")
        s.stdin = _create_mock_stdin()
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="submit", session_id="sub01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        s.stdin.write.assert_called_once_with("\r")
        assert "Submitted" in result.output


# ---------------------------------------------------------------------------
# ProcessTool — kill action
# ---------------------------------------------------------------------------
class ProcessToolKillTest(unittest.TestCase):

    def setUp(self):
        self.tool = ProcessTool()
        self.tool.registry = SessionRegistry(job_ttl_s=60)

    def test_kill_no_session(self):
        params = ProcessToolParams(action="kill", session_id="nonexistent")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "No active session" in result.error

    def test_kill_not_backgrounded(self):
        s = _create_session(session_id="k01", backgrounded=False)
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="kill", session_id="k01")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "not backgrounded" in result.error

    @patch("src.tools.process_tool.get_process_supervisor")
    def test_kill_via_supervisor_cancel(self, mock_get_supervisor):
        mock_supervisor = MagicMock()
        mock_run = MagicMock()
        mock_supervisor.get_active.return_value = mock_run
        mock_get_supervisor.return_value = mock_supervisor

        s = _create_session(session_id="k02")
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="kill", session_id="k02")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "Termination requested" in result.output
        mock_supervisor.cancel.assert_called_once_with("k02")

    @patch("src.tools.process_tool.kill_process_tree")
    @patch("src.tools.process_tool.get_process_supervisor")
    def test_kill_fallback_to_pid(self, mock_get_supervisor, mock_kill_tree):
        mock_supervisor = MagicMock()
        mock_supervisor.get_active.return_value = None
        mock_get_supervisor.return_value = mock_supervisor

        s = _create_session(session_id="k03")
        s.pid = 9999
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="kill", session_id="k03")
        result = asyncio.run(self.tool.run(params))
        assert result.success is True
        assert "Killed" in result.output
        mock_kill_tree.assert_called_once_with(9999)

    @patch("src.tools.process_tool.get_process_supervisor")
    def test_kill_no_active_run_and_no_pid(self, mock_get_supervisor):
        mock_supervisor = MagicMock()
        mock_supervisor.get_active.return_value = None
        mock_get_supervisor.return_value = mock_supervisor

        s = _create_session(session_id="k04")
        s.pid = None
        self.tool.registry.add_session(s)

        params = ProcessToolParams(action="kill", session_id="k04")
        result = asyncio.run(self.tool.run(params))
        assert result.success is False
        assert "Unable to terminate" in result.error


# ---------------------------------------------------------------------------
# ProcessTool — tool metadata
# ---------------------------------------------------------------------------
class ProcessToolMetadataTest(unittest.TestCase):

    def test_name(self):
        tool = ProcessTool()
        assert tool.name == "process"

    def test_approval_level_none(self):
        from src.tools.base_tool import ToolApprovalLevel
        tool = ProcessTool()
        assert tool.get_approval_level() == ToolApprovalLevel.NONE

    def test_params_class(self):
        tool = ProcessTool()
        assert tool.get_params_class() is ProcessToolParams
