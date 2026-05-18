import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from src.core.process.adapter_interface import (
    ManagedRun,
    RunExit,
    TerminationReason,
)
from src.core.process.session_registry import SessionRegistry
from src.tools.base_tool import ToolApprovalLevel
from src.tools.exec_tool import ExecMode, ExecTool, ExecToolParams


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
        params = ExecToolParams(command="echo hello", mode=ExecMode.ONE_SHOT)
        assert self.tool.validate_params(params) is None

    def test_mode_required(self):
        with self.assertRaises(ValidationError):
            ExecToolParams(command="echo hello")

    def test_mode_accepts_string(self):
        params = ExecToolParams(command="ls", mode="one_shot")
        assert params.mode == ExecMode.ONE_SHOT

    def test_mode_rejects_unknown(self):
        with self.assertRaises(ValidationError):
            ExecToolParams(command="ls", mode="interactive")

    def test_empty_command(self):
        params = ExecToolParams(command="   ", mode=ExecMode.ONE_SHOT)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "empty" in error.lower()

    def test_negative_timeout(self):
        params = ExecToolParams(command="echo hi", mode=ExecMode.ONE_SHOT, timeout=-1)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "positive" in error.lower()

    def test_zero_timeout(self):
        params = ExecToolParams(command="echo hi", mode=ExecMode.ONE_SHOT, timeout=0)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "positive" in error.lower()

    def test_defaults(self):
        params = ExecToolParams(command="echo hi", mode=ExecMode.ONE_SHOT)
        assert params.timeout == 120
        assert params.no_output_timeout == 60
        assert params.workdir is None

    def test_negative_no_output_timeout(self):
        params = ExecToolParams(
            command="echo hi", mode=ExecMode.ONE_SHOT, no_output_timeout=-1
        )
        error = self.tool.validate_params(params)
        assert error is not None
        assert "no_output_timeout" in error.lower()

    def test_session_with_default_timeout_allowed(self):
        params = ExecToolParams(command="bash", mode=ExecMode.SESSION)
        assert self.tool.validate_params(params) is None

    def test_session_with_explicit_non_default_timeout_rejected(self):
        params = ExecToolParams(command="bash", mode=ExecMode.SESSION, timeout=30)
        error = self.tool.validate_params(params)
        assert error is not None
        assert "one_shot" in error

    def test_daemon_with_explicit_non_default_timeout_rejected(self):
        params = ExecToolParams(
            command="openvpn --config x.ovpn", mode=ExecMode.DAEMON, timeout=300
        )
        error = self.tool.validate_params(params)
        assert error is not None
        assert "one_shot" in error

    def test_session_with_explicit_no_output_timeout_rejected(self):
        params = ExecToolParams(
            command="bash", mode=ExecMode.SESSION, no_output_timeout=30
        )
        error = self.tool.validate_params(params)
        assert error is not None
        assert "no_output_timeout" in error
        assert "one_shot" in error

    def test_daemon_with_explicit_no_output_timeout_rejected(self):
        params = ExecToolParams(
            command="openvpn --config x.ovpn",
            mode=ExecMode.DAEMON,
            no_output_timeout=120,
        )
        error = self.tool.validate_params(params)
        assert error is not None
        assert "no_output_timeout" in error


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
# ExecTool — one_shot mode: successful completion
# ---------------------------------------------------------------------------
class ExecToolOneShotSuccessTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_success(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=0)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hello", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert result.metadata["status"] == "completed"
        assert result.metadata["exit_code"] == 0

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_captures_output(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=0)
        mock_supervisor = MagicMock()

        async def fake_spawn(**kwargs):
            on_stdout = kwargs.get("on_stdout")
            if on_stdout:
                on_stdout("hello world\n")
            return managed_run

        mock_supervisor.spawn = AsyncMock(side_effect=fake_spawn)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="echo hello", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "hello world" in result.output

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_nonzero_exit_still_completed(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=1)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="false", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        # exit_code=1 with normal EXIT reason is still "completed" (not shell failure)
        assert result.success is True
        assert "exited with code 1" in result.output.lower()


# ---------------------------------------------------------------------------
# ExecTool — one_shot mode: failure cases
# ---------------------------------------------------------------------------
class ExecToolOneShotFailureTest(unittest.TestCase):

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
        params = ExecToolParams(command="nonexistent_cmd", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

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
        params = ExecToolParams(command="./not_executable", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

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
        params = ExecToolParams(
            command="sleep 999", mode=ExecMode.ONE_SHOT, timeout=30
        )
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert result.metadata["timed_out"] is True
        assert "timed out" in result.error.lower()

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
        params = ExecToolParams(command="sleep 999", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert "signal 9" in result.error.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_generic_failure_exit_2_still_completed(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(exit_code=2)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="bad_cmd", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        # exit_code=2 with EXIT reason — "completed" (not a shell failure)
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
        params = ExecToolParams(command="echo hi", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

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
        params = ExecToolParams(command="echo hi", mode=ExecMode.ONE_SHOT)
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert "Process error" in result.error


# ---------------------------------------------------------------------------
# ExecTool — session mode
# ---------------------------------------------------------------------------
class ExecToolSessionTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_returns_session_id(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(pid=5678)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="sshpass -p pw ssh user@host", mode=ExecMode.SESSION
        )
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert result.metadata["status"] == "running"
        assert result.metadata["mode"] == "session"
        assert result.metadata["pid"] == 5678
        assert "session" in result.output.lower()

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_uses_pty(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="python3 -i", mode=ExecMode.SESSION)
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["mode"] == "pty"

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_is_unbounded(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="python3 -i",
            mode=ExecMode.SESSION,
            timeout=30,
        )
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["timeout_ms"] is None

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_marks_backgrounded(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="python3 -i", mode=ExecMode.SESSION)
        result = asyncio.run(tool.run(params))

        session_id = result.metadata["session_id"]
        session = registry.get_session(session_id)
        assert session is not None
        assert session.backgrounded is True

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_does_not_wait(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="python3 -i", mode=ExecMode.SESSION)
        asyncio.run(tool.run(params))

        managed_run.wait.assert_not_called()


# ---------------------------------------------------------------------------
# ExecTool — daemon mode
# ---------------------------------------------------------------------------
class ExecToolDaemonTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_daemon_returns_session_id(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run(pid=9999)
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="openvpn --config x.ovpn", mode=ExecMode.DAEMON)
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert result.metadata["status"] == "running"
        assert result.metadata["mode"] == "daemon"
        assert result.metadata["pid"] == 9999

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_daemon_does_not_use_pty(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="openvpn --config x.ovpn", mode=ExecMode.DAEMON)
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["mode"] == "child"

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_daemon_is_unbounded(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="openvpn",
            mode=ExecMode.DAEMON,
            timeout=30,
        )
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["timeout_ms"] is None

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_daemon_does_not_wait(self, mock_get_registry, mock_get_supervisor):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="openvpn", mode=ExecMode.DAEMON)
        asyncio.run(tool.run(params))

        managed_run.wait.assert_not_called()


# ---------------------------------------------------------------------------
# ExecTool — one_shot adapter & timeout selection
# ---------------------------------------------------------------------------
class ExecToolOneShotAdapterTest(unittest.TestCase):

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_uses_child_adapter(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="ls", mode=ExecMode.ONE_SHOT)
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["mode"] == "child"

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_passes_timeout_in_ms(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="echo hi",
            mode=ExecMode.ONE_SHOT,
            timeout=30,
        )
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["timeout_ms"] == 30000

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_one_shot_passes_no_output_timeout_in_ms(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="curl https://black.hole",
            mode=ExecMode.ONE_SHOT,
            no_output_timeout=15,
        )
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["no_output_timeout_ms"] == 15000

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_session_passes_no_output_timeout_none(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(command="bash", mode=ExecMode.SESSION)
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["no_output_timeout_ms"] is None
        assert call_kwargs["timeout_ms"] is None

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_daemon_passes_no_output_timeout_none(
        self, mock_get_registry, mock_get_supervisor
    ):
        registry = SessionRegistry(job_ttl_s=60)
        mock_get_registry.return_value = registry

        managed_run, _ = _create_mock_managed_run()
        mock_supervisor = MagicMock()
        mock_supervisor.spawn = AsyncMock(return_value=managed_run)
        mock_get_supervisor.return_value = mock_supervisor

        tool = ExecTool()
        params = ExecToolParams(
            command="openvpn --config x.ovpn", mode=ExecMode.DAEMON
        )
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["no_output_timeout_ms"] is None
        assert call_kwargs["timeout_ms"] is None

    @patch("src.tools.exec_tool.get_process_supervisor")
    @patch("src.tools.exec_tool.get_session_registry")
    def test_no_output_timeout_failure_reported(
        self, mock_get_registry, mock_get_supervisor
    ):
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
        params = ExecToolParams(
            command="curl https://10.255.255.1",
            mode=ExecMode.ONE_SHOT,
            no_output_timeout=15,
        )
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert result.metadata["no_output_timed_out"] is True
        assert "no output for 15 seconds" in result.error.lower()


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
        params = ExecToolParams(
            command="ls", mode=ExecMode.SESSION, workdir="/tmp"
        )
        asyncio.run(tool.run(params))

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
        params = ExecToolParams(command="ls", mode=ExecMode.SESSION)
        asyncio.run(tool.run(params))

        call_kwargs = mock_supervisor.spawn.call_args[1]
        assert call_kwargs["cwd"] == os.getcwd()
