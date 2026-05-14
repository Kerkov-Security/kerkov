"""
Unified exec tool — one-shot and background process execution.

Spawns commands via the process supervisor with optional PTY support.
Background mode returns a session ID for follow-up via the process tool.
"""

from __future__ import annotations

import os
import time
from typing import Optional, Type

from pydantic import Field

from src.core.process.adapter_interface import TerminationReason
from src.core.process.ansi_strip import strip_ansi
from src.core.process.session_registry import (
    ProcessSession,
    get_session_registry,
)
from src.core.process.supervisor import get_process_supervisor
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult

class ExecToolParams(ToolParams):
    command: str = Field(description="Shell command to execute")
    workdir: Optional[str] = Field(
        default=None, description="Working directory (defaults to cwd)"
    )
    timeout: int = Field(
        default=120,
        description=(
            "Timeout in seconds (kills process on expiry). "
            "Ignored when `unbounded=true`."
        ),
    )
    no_output_timeout: int = Field(
        default=60,
        description=(
            "Kill process if no output for this many seconds. "
            "Ignored when `unbounded=true`."
        ),
    )
    unbounded: bool = Field(
        default=False,
        description=(
            "Disable both `timeout` and `no_output_timeout`. Required for "
            "long-lived daemons that may go quiet after startup (e.g. "
            "openvpn post-handshake, tunnels, listeners). Pair with "
            "background=true."
        ),
    )
    background: bool = Field(
        default=False,
        description=(
            "Run in background immediately, return session ID for follow-up "
            "via process tool. Timeouts still apply unless `unbounded=true`."
        ),
    )
    pty: bool = Field(
        default=True,
        description="Use pseudo-terminal (PTY) for interactive programs (REPLs, ncurses, etc.)",
    )

class ExecTool(BaseTool):
    """
    Execute shell commands with optional PTY and background support.

    Use pty=true (default) for commands that need a terminal.
    Use background=true to get a session ID for interactive follow-up
    via the process tool (poll, write, send_keys, kill, etc.).
    """

    def __init__(self):
        super().__init__(
            name="exec",
            description=(
                "Execute a shell command. Default (background=false) waits for "
                "completion — right for one-shot commands (ls, curl, grep, file ops). "
                "Use background=true for anything that doesn't terminate on its own "
                "or needs step-by-step interaction: SSH/telnet/su shells, REPLs "
                "(python, msfconsole, sqlite3), long-lived daemons (openvpn, "
                "listening servers, tunnels), or any session you'll send follow-up "
                "input to. Pair with unbounded=true when the command may go quiet "
                "after startup (openvpn post-handshake, idle listeners). "
                "Returns a session_id for the process tool "
                "(list/poll/log/write/send_keys/paste/submit/kill). "
                "pty=true (default) gives a terminal — needed for most interactive "
                "programs."
            ),
        )

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.REQUIRED

    def get_params_class(self) -> Type[ToolParams]:
        return ExecToolParams

    def validate_params(self, params: ExecToolParams) -> Optional[str]:
        if not params.command.strip():
            return "Command must not be empty"
        if params.timeout <= 0:
            return "Timeout must be positive"
        if params.no_output_timeout <= 0:
            return "no_output_timeout must be positive"
        return None

    async def run(self, params: ExecToolParams) -> ToolResult:
        registry = get_session_registry()
        supervisor = get_process_supervisor()

        session_id = registry.create_session_id()
        workdir = params.workdir or os.getcwd()
        mode = "pty" if params.pty else "child"
        timeout_ms = None if params.unbounded else params.timeout * 1000
        no_output_timeout_ms = None if params.unbounded else params.no_output_timeout * 1000

        session = ProcessSession(
            id=session_id,
            command=params.command,
            started_at=time.time(),
            cwd=workdir,
        )
        registry.add_session(session)

        # Output callbacks that feed into the registry
        def handle_stdout(chunk: str) -> None:
            registry.append_output(session, "stdout", chunk)

        def handle_stderr(chunk: str) -> None:
            registry.append_output(session, "stderr", chunk)

        try:
            managed_run = await supervisor.spawn(
                command=params.command,
                mode=mode,
                run_id=session_id,
                cwd=workdir,
                timeout_ms=timeout_ms,
                no_output_timeout_ms=no_output_timeout_ms,
                on_stdout=handle_stdout,
                on_stderr=handle_stderr,
            )
        except Exception as e:
            registry.mark_exited(session, None, None, "failed")
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to spawn process: {e}",
                metadata={"command": params.command},
            )

        # Wire stdin and pid from the managed run to the session
        session.stdin = managed_run.stdin
        session.pid = managed_run.pid

        # --- Background mode ---
        if params.background:
            registry.mark_backgrounded(session)
            return ToolResult(
                success=True,
                output=(
                    f"Command running in background (session {session_id}, "
                    f"pid {session.pid or 'n/a'}). "
                    f"Use process tool (list/poll/log/write/send_keys/paste/submit/kill) for follow-up."
                ),
                metadata={
                    "status": "running",
                    "session_id": session_id,
                    "pid": session.pid,
                    "started_at": session.started_at,
                    "cwd": workdir,
                },
            )

        # --- Foreground mode: wait for completion ---
        try:
            run_exit = await managed_run.wait()
        except Exception as e:
            registry.mark_exited(session, None, None, "failed")
            aggregated = strip_ansi(session.aggregated).strip()
            return ToolResult(
                success=False,
                output=aggregated,
                error=f"Process error: {e}",
                metadata={"command": params.command},
            )

        aggregated = strip_ansi(session.aggregated).strip()

        # Determine status
        is_normal_exit = run_exit.reason == TerminationReason.EXIT
        exit_code = run_exit.exit_code if run_exit.exit_code is not None else 0
        is_shell_failure = exit_code in (126, 127)
        status = "completed" if (is_normal_exit and not is_shell_failure) else "failed"

        registry.mark_exited(session, run_exit.exit_code, run_exit.exit_signal, status)

        if status == "completed":
            exit_msg = f"\n\n(Command exited with code {exit_code})" if exit_code != 0 else ""
            return ToolResult(
                success=True,
                output=aggregated + exit_msg,
                metadata={
                    "status": "completed",
                    "exit_code": exit_code,
                    "duration_ms": run_exit.duration_ms,
                    "cwd": workdir,
                },
            )

        # Failed
        if is_shell_failure:
            reason = "Command not found" if exit_code == 127 else "Command not executable (permission denied)"
        elif run_exit.no_output_timed_out:
            reason = "Command timed out waiting for output"
        elif run_exit.timed_out:
            reason = (
                "Command timed out"
                if params.unbounded
                else f"Command timed out after {params.timeout} seconds"
            )
        elif run_exit.exit_signal is not None:
            reason = f"Command aborted by signal {run_exit.exit_signal}"
        else:
            reason = f"Command failed with exit code {exit_code}"

        error_msg = f"{aggregated}\n\n{reason}" if aggregated else reason

        return ToolResult(
            success=False,
            output=aggregated,
            error=error_msg,
            metadata={
                "status": "failed",
                "exit_code": run_exit.exit_code,
                "exit_signal": run_exit.exit_signal,
                "duration_ms": run_exit.duration_ms,
                "timed_out": run_exit.timed_out,
                "cwd": workdir,
            },
        )
