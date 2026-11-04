import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kerkov.core.process.adapter_interface import (
    ManagedRun,
    RunExit,
    TerminationReason,
)
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.tools.exec_tool import ExecMode, ExecStdio, ExecTool, ExecToolParams

ONE_SHOT, SESSION, DAEMON = ExecMode.ONE_SHOT, ExecMode.SESSION, ExecMode.DAEMON


def _create_mock_managed_run(
    *,
    pid: int = 1234,
    stdio_mode: str = "pty",
    exit_code: Optional[int] = 0,
    exit_signal: Optional[int] = None,
    reason: TerminationReason = TerminationReason.EXIT,
    timed_out: bool = False,
    no_output_timed_out: bool = False,
    pending: bool = False,
) -> ManagedRun:
    run_exit = RunExit(
        reason=reason,
        exit_code=exit_code,
        exit_signal=exit_signal,
        duration_ms=100.0,
        stdout="",
        stderr="",
        timed_out=timed_out,
        no_output_timed_out=no_output_timed_out,
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    return ManagedRun(
        run_id="test01",
        pid=pid,
        started_at_ms=time.time() * 1000,
        stdio_mode=stdio_mode,
        stdin=MagicMock(),
        wait=(
            AsyncMock(side_effect=wait_forever)
            if pending
            else AsyncMock(return_value=run_exit)
        ),
        cancel=MagicMock(),
    )


def _create_exec_tool(
    registry: Optional[SessionRegistry] = None,
    supervisor: Optional[Any] = None,
) -> ExecTool:
    return ExecTool(execution_enabled=True, registry=registry, supervisor=supervisor)


def _run_exec(
    params: ExecToolParams,
    managed_run: Optional[ManagedRun] = None,
    *,
    spawn: Optional[AsyncMock] = None,
    settle: bool = False,
):
    """Run one call against a fake supervisor; return (result, registry, spawn kwargs)."""
    registry = SessionRegistry(job_ttl_s=60)
    supervisor = MagicMock()
    supervisor.spawn = spawn or AsyncMock(
        return_value=managed_run or _create_mock_managed_run()
    )
    tool = _create_exec_tool(registry=registry, supervisor=supervisor)

    async def scenario():
        result = await tool.run(params)
        if settle:
            # Let the background wait task for session/daemon runs finish.
            await asyncio.sleep(0)
        return result

    result = asyncio.run(scenario())
    return result, registry, supervisor.spawn.call_args.kwargs


def _params(mode: ExecMode = ONE_SHOT, command: str = "echo hi", **kwargs):
    return ExecToolParams(command=command, mode=mode, **kwargs)


@pytest.mark.parametrize(
    "params,expected_fragments",
    [
        (_params(command="   "), ["empty"]),
        (_params(timeout=0), ["positive"]),
        (_params(timeout=-1), ["positive"]),
        (_params(no_output_timeout=-1), ["no_output_timeout"]),
        (_params(SESSION, timeout=30), ["one_shot"]),
        (_params(DAEMON, timeout=300), ["one_shot"]),
        (_params(SESSION, no_output_timeout=30), ["no_output_timeout", "one_shot"]),
        (_params(DAEMON, no_output_timeout=120), ["no_output_timeout"]),
        (_params(SESSION, stdio=ExecStdio.PIPE), ["session", "pty"]),
    ],
)
def test_validate_params_rejects(params, expected_fragments) -> None:
    error = _create_exec_tool().validate_params(params)

    assert error is not None
    for fragment in expected_fragments:
        assert fragment in error.lower()


@pytest.mark.parametrize("params", [_params(), _params(SESSION), _params(DAEMON)])
def test_validate_params_accepts_defaults(params) -> None:
    assert _create_exec_tool().validate_params(params) is None


@pytest.mark.parametrize("mode", list(ExecMode))
def test_disabled_by_default_before_any_process_or_session_side_effect(mode) -> None:
    registry = MagicMock(spec=SessionRegistry)
    supervisor = MagicMock(spec=ProcessSupervisor)
    supervisor.spawn = AsyncMock()
    tool = ExecTool(registry=registry, supervisor=supervisor)

    result = asyncio.run(tool.run(_params(mode, command="echo blocked")))

    assert result.success is False
    assert result.metadata == {"policy": "execution_disabled", "mode": mode.value}
    assert "EXECUTION_ENABLED=true" in result.error
    registry.create_session_id.assert_not_called()
    registry.add_session.assert_not_called()
    supervisor.spawn.assert_not_awaited()


@patch.dict(
    os.environ,
    {
        "PATH": "/usr/bin",
        "GOOGLE_API_KEY": "must-not-reach-child",
        "SEARCH_API_KEY": "must-not-reach-child",
    },
    clear=True,
)
def test_spawn_environment_excludes_managed_credentials() -> None:
    result, _, spawn_kwargs = _run_exec(_params(command="env"))

    assert result.success is True
    assert spawn_kwargs["env"] == {"PATH": "/usr/bin"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY behavior")
@patch.dict(os.environ, {"GOOGLE_API_KEY": "must-not-reach-child"}, clear=True)
def test_empty_sanitized_environment_does_not_reinherit_parent() -> None:
    result = asyncio.run(
        _create_exec_tool().run(
            _params(command='printf "%s" "${GOOGLE_API_KEY-unset}"')
        )
    )

    assert result.success is True
    assert result.output == "unset"
    assert "must-not-reach-child" not in str(result)


def test_one_shot_captures_output() -> None:
    async def fake_spawn(**kwargs):
        kwargs["on_stdout"]("hello world\n")
        return _create_mock_managed_run()

    result, _, _ = _run_exec(_params(), spawn=AsyncMock(side_effect=fake_spawn))

    assert result.success is True
    assert result.metadata["status"] == "completed"
    assert "hello world" in result.output


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_ordinary_exit_codes_complete(exit_code: int) -> None:
    result, _, _ = _run_exec(_params(), _create_mock_managed_run(exit_code=exit_code))

    assert result.success is True
    assert result.metadata["status"] == "completed"
    assert result.metadata["exit_code"] == exit_code
    if exit_code:
        assert f"exited with code {exit_code}" in result.output.lower()


@pytest.mark.parametrize(
    "run_kwargs,error_fragment",
    [
        ({"exit_code": 127}, "not found"),
        ({"exit_code": 126}, "permission denied"),
        (
            {"exit_code": None, "exit_signal": 9, "reason": TerminationReason.SIGNAL},
            "signal 9",
        ),
        (
            {
                "exit_code": None,
                "reason": TerminationReason.OVERALL_TIMEOUT,
                "timed_out": True,
            },
            "timed out",
        ),
        (
            {
                "exit_code": None,
                "reason": TerminationReason.NO_OUTPUT_TIMEOUT,
                "timed_out": True,
                "no_output_timed_out": True,
            },
            "no output for 15 seconds",
        ),
    ],
)
def test_one_shot_failures(run_kwargs, error_fragment: str) -> None:
    result, _, _ = _run_exec(
        _params(no_output_timeout=15), _create_mock_managed_run(**run_kwargs)
    )

    assert result.success is False
    assert result.metadata["status"] == "failed"
    assert error_fragment in result.error.lower()
    assert result.metadata["timed_out"] is run_kwargs.get("timed_out", False)


def test_spawn_exception_is_reported() -> None:
    result, _, _ = _run_exec(
        _params(), spawn=AsyncMock(side_effect=OSError("cannot spawn"))
    )

    assert result.success is False
    assert "Failed to spawn" in result.error


def test_wait_exception_is_reported() -> None:
    managed_run = _create_mock_managed_run()
    managed_run.wait = AsyncMock(side_effect=RuntimeError("boom"))

    result, _, _ = _run_exec(_params(), managed_run)

    assert result.success is False
    assert "Process error" in result.error


def test_cancelled_spawn_clears_registry_session() -> None:
    registry = SessionRegistry(job_ttl_s=60)
    registry.create_session_id = MagicMock(return_value="spawncancel")
    supervisor = MagicMock()

    async def scenario():
        started = asyncio.Event()

        async def blocked_spawn(**kwargs):
            started.set()
            await asyncio.Event().wait()

        supervisor.spawn = AsyncMock(side_effect=blocked_spawn)
        tool = _create_exec_tool(registry=registry, supervisor=supervisor)
        task = asyncio.create_task(tool.run(_params(command="sleep 999")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert registry.get_session("spawncancel") is None


def test_cancelling_one_shot_kills_run_and_clears_registry() -> None:
    registry = SessionRegistry(job_ttl_s=60)
    registry.create_session_id = MagicMock(return_value="cancel01")
    managed_run = _create_mock_managed_run()

    async def scenario():
        waiting = asyncio.Event()

        async def wait_forever():
            waiting.set()
            await asyncio.Event().wait()

        managed_run.wait = AsyncMock(side_effect=wait_forever)
        supervisor = MagicMock()
        supervisor.spawn = AsyncMock(return_value=managed_run)
        tool = _create_exec_tool(registry=registry, supervisor=supervisor)
        task = asyncio.create_task(tool.run(_params(command="sleep 999")))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    managed_run.cancel.assert_called_once_with(TerminationReason.MANUAL_CANCEL)
    assert registry.get_session("cancel01") is None


@pytest.mark.parametrize(
    "params,adapter,stdio",
    [
        (_params(), "pty", "pty"),
        (_params(stdio=ExecStdio.PIPE), "child", "pipe"),
        (_params(DAEMON), "pty", "pty"),
        (_params(DAEMON, stdio=ExecStdio.PIPE), "child", "pipe"),
        (_params(SESSION), "pty", "pty"),
    ],
)
def test_stdio_selects_adapter(params, adapter: str, stdio: str) -> None:
    # A one-shot call awaits its run, so only background modes may stay pending.
    managed_run = _create_mock_managed_run(
        stdio_mode=stdio, pending=params.mode is not ONE_SHOT
    )

    result, _, spawn_kwargs = _run_exec(params, managed_run)

    assert spawn_kwargs["mode"] == adapter
    assert result.metadata["stdio"] == stdio


def test_session_forces_pty_even_if_pipe_requested() -> None:
    # validate_params rejects this; run() must still never hand a session a pipe.
    params = _params(SESSION, stdio=ExecStdio.PIPE)
    managed_run = _create_mock_managed_run(stdio_mode="pty", pending=True)

    result, _, spawn_kwargs = _run_exec(params, managed_run)

    assert spawn_kwargs["mode"] == "pty"
    assert result.metadata["stdio"] == "pty"


def test_one_shot_passes_timeouts_in_ms() -> None:
    _, _, spawn_kwargs = _run_exec(_params(timeout=30, no_output_timeout=15))

    assert spawn_kwargs["timeout_ms"] == 30000
    assert spawn_kwargs["no_output_timeout_ms"] == 15000


@pytest.mark.parametrize("mode", [SESSION, DAEMON])
def test_background_modes_are_unbounded(mode: ExecMode) -> None:
    managed_run = _create_mock_managed_run(pending=True)

    _, _, spawn_kwargs = _run_exec(_params(mode, timeout=30), managed_run)

    assert spawn_kwargs["timeout_ms"] is None
    assert spawn_kwargs["no_output_timeout_ms"] is None


@pytest.mark.parametrize("mode", [SESSION, DAEMON])
def test_background_modes_return_while_still_running(mode: ExecMode) -> None:
    managed_run = _create_mock_managed_run(pid=5678, pending=True)

    result, registry, _ = _run_exec(_params(mode), managed_run, settle=True)

    assert result.success is True
    assert result.metadata["status"] == "running"
    assert result.metadata["mode"] == mode.value
    assert result.metadata["pid"] == 5678
    assert managed_run.wait.await_count == 1
    session = registry.get_session(result.metadata["session_id"])
    assert session is not None
    assert session.backgrounded is True


@pytest.mark.parametrize(
    "mode,run_kwargs,status",
    [
        (SESSION, {"exit_code": 0}, "completed"),
        (DAEMON, {"exit_code": 1}, "failed"),
        (
            SESSION,
            {"exit_code": None, "reason": TerminationReason.MANUAL_CANCEL},
            "killed",
        ),
    ],
)
def test_background_wait_records_finished_status(mode, run_kwargs, status) -> None:
    managed_run = _create_mock_managed_run(**run_kwargs)

    result, registry, _ = _run_exec(_params(mode), managed_run, settle=True)

    session_id = result.metadata["session_id"]
    assert registry.get_session(session_id) is None
    finished = registry.get_finished_session(session_id)
    assert finished is not None
    assert finished.status == status
    assert finished.exit_code == run_kwargs["exit_code"]
    assert finished.exit_signal is None


def _isatty_command() -> str:
    # Double-quoted form parses identically under bash and cmd, so this
    # integration test runs on both POSIX and Windows.
    return f'"{sys.executable}" -c "import sys; print(sys.stdout.isatty())"'


@pytest.mark.parametrize(
    "stdio,expected", [(ExecStdio.AUTO, "true"), (ExecStdio.PIPE, "false")]
)
def test_real_process_sees_tty_only_under_pty(stdio: ExecStdio, expected) -> None:
    tool = _create_exec_tool(
        registry=SessionRegistry(job_ttl_s=60), supervisor=ProcessSupervisor()
    )
    params = _params(
        command=_isatty_command(), stdio=stdio, timeout=5, no_output_timeout=5
    )

    result = asyncio.run(tool.run(params))

    assert result.success is True
    assert expected in result.output.lower()


def test_custom_workdir() -> None:
    _, _, spawn_kwargs = _run_exec(_params(SESSION, workdir="/tmp"))

    assert spawn_kwargs["cwd"] == "/tmp"


def test_default_workdir_is_cwd() -> None:
    _, _, spawn_kwargs = _run_exec(_params(SESSION))

    assert spawn_kwargs["cwd"] == os.getcwd()


def test_default_workdir_is_workspace_when_active(tmp_path: Path) -> None:
    reset_token = set_current_workspace(None)
    try:
        workspace = RunWorkspaceManager(tmp_path / "workspaces").activate("eng123")

        _, _, spawn_kwargs = _run_exec(_params(SESSION))
    finally:
        reset_current_workspace(reset_token)

    assert spawn_kwargs["cwd"] == str(workspace)
