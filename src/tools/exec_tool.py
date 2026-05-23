"""
Unified exec tool — one-shot, interactive session, and daemon execution.

Spawns commands via the process supervisor. The caller picks a `mode`
expressing intent; the tool derives PTY usage, background behavior, and
timeout semantics from that. Session/daemon modes return a session_id for
follow-up via the process tool.
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Optional, Type

from pydantic import Field

from src.core.process.adapter_interface import TerminationReason
from src.core.process.ansi_strip import strip_ansi
from src.core.process.session_registry import (
    ProcessSession,
    get_session_registry,
)
from src.core.process.supervisor import get_process_supervisor
from src.tools.base_tool import BaseTool, ToolParams, ToolResult


class ExecMode(str, Enum):
    ONE_SHOT = "one_shot"
    SESSION = "session"
    DAEMON = "daemon"


DEFAULT_ONE_SHOT_TIMEOUT_SECONDS = 120
DEFAULT_NO_OUTPUT_TIMEOUT_SECONDS = 60


class ExecToolParams(ToolParams):
    command: str = Field(description="Shell command to execute")
    mode: ExecMode = Field(
        description=(
            "What kind of work this command is. Pick by intent, not by binary:\n"
            "- one_shot: Runs and exits on its own. Use when you only need the "
            "final output (ls, curl, grep, file ops, scripts that print and quit).\n"
            "- session: Opens something you will keep talking to. Use whenever "
            "you plan to send follow-up commands or input — SSH/sshpass/telnet/su "
            "shells, REPLs (python, msfconsole, sqlite3), or anything that needs "
            "a terminal (sudo password prompts, vim, less). Allocates a PTY and "
            "returns a session_id; drive it via the process tool "
            "(paste/submit/poll/log/kill).\n"
            "- daemon: Long-lived process you start and leave running. Use for "
            "openvpn, tunnels, listening servers — things that don't terminate "
            "on their own and don't need stdin. Returns a session_id for "
            "poll/log/kill."
        )
    )
    workdir: Optional[str] = Field(
        default=None, description="Working directory (defaults to cwd)"
    )
    timeout: int = Field(
        default=DEFAULT_ONE_SHOT_TIMEOUT_SECONDS,
        description=(
            "Overall timeout in seconds (kills process on expiry). "
            "Applies only to mode=one_shot; session and daemon modes are "
            "unbounded and reject non-default values."
        ),
    )
    no_output_timeout: int = Field(
        default=DEFAULT_NO_OUTPUT_TIMEOUT_SECONDS,
        description=(
            "Kill the process if it produces no output for this many "
            "seconds. Applies only to mode=one_shot — fail-fast guard for "
            "stuck network calls and hung subprocesses. Session and daemon "
            "modes ignore it (interactive sessions pause between commands; "
            "daemons like openvpn go quiet after the handshake by design)."
        ),
    )


class ExecTool(BaseTool):
    """
    Execute shell commands. Caller picks a mode expressing intent:
    one_shot (run-and-wait), session (interactive PTY you'll drive),
    daemon (fire-and-forget long-lived process).
    """

    def __init__(self):
        super().__init__(
            name="exec",
            description=(
                "Execute a shell command. The required `mode` parameter expresses "
                "intent and selects the right execution shape:\n"
                "- one_shot — runs to completion, returns output. Right for "
                "ls/curl/grep/scripts that print-and-quit.\n"
                "- session — opens an interactive PTY session you'll keep talking "
                "to. Required for SSH/sshpass shells, REPLs (python, msfconsole), "
                "and anything that needs a terminal (sudo, vim, less). Returns a "
                "session_id; drive it via the process tool "
                "(paste/submit/poll/log/send_keys/kill).\n"
                "- daemon — long-lived process you start and leave running "
                "(openvpn, tunnels, listening servers). Returns a session_id; no "
                "stdin expected.\n"
                "If you'll send a second command to the same target, you want "
                "session — not a second one_shot."
            ),
        )

    def get_params_class(self) -> Type[ToolParams]:
        return ExecToolParams

    def validate_params(self, params: ExecToolParams) -> Optional[str]:
        if not params.command.strip():
            return "Command must not be empty"
        if params.timeout <= 0:
            return "Timeout must be positive"
        if params.no_output_timeout <= 0:
            return "no_output_timeout must be positive"
        if params.mode != ExecMode.ONE_SHOT:
            if params.timeout != DEFAULT_ONE_SHOT_TIMEOUT_SECONDS:
                return (
                    f"`timeout` only applies to mode=one_shot. "
                    f"mode={params.mode.value} ignores it — drop the param "
                    f"or switch to mode=one_shot."
                )
            if params.no_output_timeout != DEFAULT_NO_OUTPUT_TIMEOUT_SECONDS:
                return (
                    f"`no_output_timeout` only applies to mode=one_shot. "
                    f"mode={params.mode.value} ignores it — drop the param "
                    f"or switch to mode=one_shot."
                )
        return None

    async def run(self, params: ExecToolParams) -> ToolResult:
        registry = get_session_registry()
        supervisor = get_process_supervisor()

        is_session = params.mode == ExecMode.SESSION
        is_daemon = params.mode == ExecMode.DAEMON
        is_background = is_session or is_daemon
        is_unbounded = is_background

        session_id = registry.create_session_id()
        workdir = params.workdir or os.getcwd()
        spawn_mode = "pty" if is_session else "child"
        timeout_ms = None if is_unbounded else params.timeout * 1000
        no_output_timeout_ms = (
            None if is_unbounded else params.no_output_timeout * 1000
        )

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
                mode=spawn_mode,
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

        # --- Background modes (session / daemon) ---
        if is_background:
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
                    "mode": params.mode.value,
                },
            )

        # --- One-shot mode: wait for completion ---
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
            reason = (
                f"Command produced no output for {params.no_output_timeout} "
                f"seconds and was killed"
            )
        elif run_exit.timed_out:
            reason = f"Command timed out after {params.timeout} seconds"
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
                "no_output_timed_out": run_exit.no_output_timed_out,
                "cwd": workdir,
            },
        )
