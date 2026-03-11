import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.process.adapter_interface import (
    ManagedRun,
    RunExit,
    TerminationReason,
)
from src.core.process.session_registry import SessionRegistry
from src.tools.base_tool import ToolApprovalLevel, ToolResult
from src.tools.exec_tool import ExecTool, ExecToolParams


def _create_mock_managed_run(
    run_id: str = "test01",
    pid: int = 1234,
    exit_code: int = 0,
    exit_signal: int = None,
    reason: TerminationReason = TerminationReason.EXIT,
    timed_out: bool = False,
    no_output_timed_out: bool = False,
    duration_ms: float = 100.0,
    stdout: str = "",
    stderr: str = "",
):
    run_exit = RunExit(
        reason=reason,
        exit_code=exit_code,
        exit_signal=exit_signal,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        no_output_timed_out=no_output_timed_out,
    )
    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdin.close = MagicMock()

    managed_run = ManagedRun(
        run_id=run_id,
        pid=pid,
        started_at_ms=time.time() * 1000,
        stdin=mock_stdin,
        wait=AsyncMock(return_value=run_exit),
        cancel=MagicMock(),
    )
    return managed_run, run_exit


# ---------------------------------------------------------------------------
# ExecToolParams validation
# ---------------------------------------------------------------------------
class ExecToolParamsValidationTest(unittest.TestCase):

    def setUp(self):
        self.tool = ExecTool()

    def test_valid_params(self):
        params = ExecToolParams(command="echo hello")
        assert self.tool.validate_params(params) is None

    def test_empty_command(self):
        params = ExecToolParams(command="   ")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "empty" in error.lower()

    def test_negative_timeout(self):
        params = ExecToolParams(command="echo hi", timeout=-1)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "positive" in error.lower()

    def test_zero_timeout(self):
        params = ExecToolParams(command="echo hi", timeout=0)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "positive" in error.lower()

    def test_negative_no_output_timeout(self):
        params = ExecToolParams(command="echo hi", no_output_timeout=-1)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "positive" in error.lower()

    def test_none_timeouts_allowed(self):
        params = ExecToolParams(command="echo hi", timeout=None, no_output_timeout=None)
        assert self.tool.validate_params(params) is None

    def test_defaults(self):
        params = ExecToolParams(command="echo hi")
        assert params.timeout == 120
        assert params.no_output_timeout == 60
        assert params.background is False
        assert params.pty is True
        assert params.workdir is None


# ---------------------------------------------------------------------------
# ExecTool — metadata
# ---------------------------------------------------------------------------
class ExecToolMetadataTest(unittest.TestCase):

    def test_name(self):
        tool = ExecTool()
        assert tool.name == "exec"

    def test_approval_level_required(self):
        tool = ExecTool()
        assert tool.get_approval_level() == ToolApprovalLevel.REQUIRED

    def test_params_class(self):
        tool = ExecTool()
        assert tool.get_params_class() is ExecToolParams


# ---------------------------------------------------------------------------
# ExecTool — foreground mode: successful completion
# ---------------------------------------------------------------------------
class ExecToolForegroundSuccessTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_foreground_success(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=0)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hello")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is True
        assert result.metadata["status"] == "completed"
        assert result.metadata["exit_code"] == 0

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_foreground_captures_output(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=0)
        mock_supervisor = MagicMock()

        async def fake_spawn(**kwargs):
            # Simulate output via callbacks
            on_stdout = kwargs.get("on_stdout")
            if on_stdout:
                on_stdout("hello world\n")
            return managed_run

        mock_supervisor.spawn = AsyncMock(side_effect=fake_spawn)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hello")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is True
        assert "hello world" in result.output

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_foreground_nonzero_exit_still_completed(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=1)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="false")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        # exit_code=1 with normal EXIT reason is still "completed" (not shell failure)
        assert result.success is True
        assert "exited with code 1" in result.output.lower()


# ---------------------------------------------------------------------------
# ExecTool — foreground mode: failure cases
# ---------------------------------------------------------------------------
class ExecToolForegroundFailureTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_command_not_found_127(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=127)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="nonexistent_cmd")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert result.metadata["status"] == "failed"
        assert "not found" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_permission_denied_126(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=126)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="./not_executable")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert "permission denied" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_overall_timeout(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(
            exit_code=None,
            reason=TerminationReason.OVERALL_TIMEOUT,
            timed_out=True,
        )
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="sleep 999", timeout=30)
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert result.metadata["timed_out"] is True
        assert "timed out" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_no_output_timeout(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(
            exit_code=None,
            reason=TerminationReason.NO_OUTPUT_TIMEOUT,
            timed_out=True,
            no_output_timed_out=True,
        )
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="hang", no_output_timeout=10)
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert "timed out waiting for output" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_killed_by_signal(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(
            exit_code=None,
            exit_signal=9,
            reason=TerminationReason.SIGNAL,
        )
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="sleep 999")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert "signal 9" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_generic_failure(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=2)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="bad_cmd")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        # exit_code=2 with EXIT reason — "completed" (not a shell failure)
        # Actually exit_code=2 is normal EXIT, not shell failure, so it's completed
        assert result.success is True


# ---------------------------------------------------------------------------
# ExecTool — spawn failure
# ---------------------------------------------------------------------------
class ExecToolSpawnFailureTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_spawn_exception(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(side_effect=OSError("cannot spawn"))
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hi")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert "Failed to spawn" in result.error

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_wait_exception(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        managed_run.wait = AsyncMock(side_effect=RuntimeError("boom"))
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hi")
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is False
        assert "Process error" in result.error


# ---------------------------------------------------------------------------
# ExecTool — background mode
# ---------------------------------------------------------------------------
class ExecToolBackgroundTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_background_returns_session_id(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(pid=5678)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="sleep 999", background=True)
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        assert result.success is True
        assert result.metadata["status"] == "running"
        assert "session" in result.output.lower()
        assert result.metadata["pid"] == 5678

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_background_marks_session_backgrounded(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="sleep 999", background=True)
        result = asyncio.get_event_loop().run_until_complete(tool.run(params))

        session_id = result.metadata["session_id"]
        session = registry.get_session(session_id)
        assert session is not None
        assert session.backgrounded is True

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_background_does_not_wait(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="sleep 999", background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        managed_run.wait.assert_not_called()


# ---------------------------------------------------------------------------
# ExecTool — mode selection
# ---------------------------------------------------------------------------
class ExecToolModeSelectionTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_pty_true_passes_pty_mode(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="vim", pty=True, background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["mode"] == "pty"

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_pty_false_passes_child_mode(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="ls", pty=False, background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["mode"] == "child"


# ---------------------------------------------------------------------------
# ExecTool — timeout_ms conversion
# ---------------------------------------------------------------------------
class ExecToolTimeoutConversionTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_timeout_seconds_to_ms(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hi", timeout=30, no_output_timeout=15, background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["timeout_ms"] == 30000
        assert call_kwargs["no_output_timeout_ms"] == 15000

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_none_timeout_passes_none(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hi", timeout=None, no_output_timeout=None, background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["timeout_ms"] is None
        assert call_kwargs["no_output_timeout_ms"] is None


# ---------------------------------------------------------------------------
# ExecTool — workdir
# ---------------------------------------------------------------------------
class ExecToolWorkdirTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_custom_workdir(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="ls", workdir="/tmp", background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["cwd"] == "/tmp"

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_default_workdir_is_cwd(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="ls", background=True)
        asyncio.get_event_loop().run_until_complete(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["cwd"] == os.getcwd()
