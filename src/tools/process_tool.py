"""
Unified process management tool — manage running/backgrounded exec sessions.

Provides list, poll, log, write, send_keys, paste, submit, and kill actions
for interacting with backgrounded sessions started by the exec tool.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Type

from pydantic import Field

from src.core.process.ansi_strip import strip_ansi
from src.core.process.key_encoder import (
    KeyEncodingRequest,
    encode_key_sequence,
    encode_paste,
)
from src.core.process.kill_tree import kill_process_tree
from src.core.process.session_registry import (
    ProcessSession,
    FinishedSession,
    SessionRegistry,
    get_session_registry,
)
from src.core.process.supervisor import get_process_supervisor
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult

DEFAULT_LOG_TAIL_LINES = 200
MAX_POLL_WAIT_MS = 120_000

class ProcessToolParams(ToolParams):
    action: str = Field(
        description="Action: list, poll, log, write, send_keys, paste, submit, kill"
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Session ID (required for all actions except list)",
    )
    data: Optional[str] = Field(
        default=None,
        description=(
            "Raw data to send to stdin (for write action). "
            "Only use for non-PTY/pipe sessions. For PTY sessions, use 'paste' instead."
        ),
    )
    keys: Optional[list[str]] = Field(
        default=None,
        description=(
            "Special key tokens (for send_keys). Do NOT use this to type text — use 'write' instead. "
            "Only for control/special keys: ['C-c'] (Ctrl+C/interrupt), ['Up'], ['Down'], "
            "['Tab'], ['Escape'], ['C-a'], ['M-x'], ['S-Tab'], ['F1']-['F12']."
        ),
    )
    hex_bytes: Optional[list[str]] = Field(
        default=None, description="Hex bytes to send (for send_keys, e.g. ['0x04'])"
    )
    literal: Optional[str] = Field(
        default=None, description="Literal string to send (for send_keys)"
    )
    text: Optional[str] = Field(
        default=None,
        description=(
            "Text to paste into the session (for paste action). "
            "This is the preferred way to send commands to PTY sessions. "
            "Use 'submit' after to press Enter. Works for single or multi-line input."
        ),
    )
    bracketed: bool = Field(default=True, description="Use bracketed paste mode (for paste action)")
    eof: bool = Field(default=False, description="Close stdin after write")
    offset: Optional[int] = Field(default=None, description="Log line offset (for log action)")
    limit: Optional[int] = Field(default=None, description="Log line limit (for log action)")
    timeout: Optional[int] = Field(
        default=None,
        description="Poll wait timeout in milliseconds (for poll action)",
    )

class ProcessTool(BaseTool):
    """
    Manage running/backgrounded exec sessions.

    Actions: list, poll, log, write, send_keys, paste, submit, kill.
    """

    def __init__(self):
        super().__init__(
            name="process",
            description=(
                "Manage running/backgrounded exec sessions. "
                "To send commands to a PTY session, use 'paste' with text then 'submit' to press Enter. "
                "Example: paste text='import sys' then submit. "
                "ONLY use 'send_keys' for control/special keys (Ctrl+C, arrows, Escape), NOT for typing text. "
                "Use 'write' only for raw pipe/non-PTY sessions. "
                "Actions: "
                "list (show all sessions), "
                "poll (get new output, optionally wait for more), "
                "log (read full output history with pagination), "
                "paste (send text to the session — preferred for PTY), "
                "submit (press Enter), "
                "write (send raw data to stdin — for non-PTY/pipe sessions only), "
                "send_keys (ONLY for special keys: C-c, Up, Down, Tab, Escape, F1-F12), "
                "kill (terminate session)."
            ),
        )
        self.registry: SessionRegistry = get_session_registry()

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.NONE

    def get_params_class(self) -> Type[ToolParams]:
        return ProcessToolParams

    def validate_params(self, params: ProcessToolParams) -> Optional[str]:
        valid_actions = {
            "list", "poll", "log", "write", "send_keys", "paste", "submit", "kill",
        }
        if params.action not in valid_actions:
            return (
                f"Unknown action '{params.action}'. "
                f"Must be one of: {', '.join(sorted(valid_actions))}"
            )
        if params.action != "list" and not params.session_id:
            return "session_id is required for all actions except list"
        return None

    async def run(self, params: ProcessToolParams) -> ToolResult:
        if params.action == "list":
            return self._handle_list()

        runningSession = self.registry.get_session(params.session_id)  # type: ignore[arg-type]
        finishedSession = self.registry.get_finished_session(params.session_id)  # type: ignore[arg-type]

        if params.action == "poll":
            return await self._handle_poll(params, runningSession, finishedSession)
        elif params.action == "log":
            return self._handle_log(params, runningSession, finishedSession)
        elif params.action == "write":
            return self._handle_write(params, runningSession)
        elif params.action == "send_keys":
            return self._handle_send_keys(params, runningSession)
        elif params.action == "paste":
            return self._handle_paste(params, runningSession)
        elif params.action == "submit":
            return self._handle_submit(params, runningSession)
        elif params.action == "kill":
            return self._handle_kill(params, runningSession)

        return ToolResult(
            success=False, output="", error=f"Unknown action: {params.action}"
        )

    # --- Action handlers ---

    def _handle_list(self) -> ToolResult:
        running = self.registry.list_running_sessions()
        finished = self.registry.list_finished_sessions()

        lines: list[str] = []
        for s in running:
            runtime_s = time.time() - s.started_at
            lines.append(
                f"{s.id}  running   {_format_duration(runtime_s)}  "
                f"pid={s.pid or 'n/a'}  {_truncate(s.command, 80)}"
            )
        for s in finished:
            runtime_s = s.ended_at - s.started_at
            status = s.status.ljust(9)
            lines.append(
                f"{s.id}  {status}  {_format_duration(runtime_s)}  "
                f"{_truncate(s.command, 80)}"
            )

        return ToolResult(
            success=True,
            output="\n".join(lines) if lines else "No running or recent sessions.",
            metadata={"running_count": len(running), "finished_count": len(finished)},
        )

    async def _handle_poll(
        self, 
        params: ProcessToolParams, 
        runningSession: Optional[ProcessSession], 
        finishedSession: Optional[FinishedSession], 
    ) -> ToolResult:
        sid = params.session_id

        if runningSession is None:
            if finishedSession is not None:
                exit_label = (
                    f"signal {finishedSession.exit_signal}"
                    if finishedSession.exit_signal
                    else f"code {finishedSession.exit_code or 0}"
                )
                text = strip_ansi(finishedSession.tail) if finishedSession.tail else "(no output recorded)"
                return ToolResult(
                    success=True,
                    output=f"{text}\n\nProcess exited with {exit_label}.",
                    metadata={
                        "status": finishedSession.status,
                        "session_id": sid,
                        "exit_code": finishedSession.exit_code,
                    },
                )
            return ToolResult(
                success=False, output="", error=f"No session found for {sid}"
            )

        if not runningSession.backgrounded:
            return ToolResult(
                success=False, output="", error=f"Session {sid} is not backgrounded."
            )

        poll_wait_ms = _resolve_poll_wait_ms(params.timeout)
        if poll_wait_ms > 0 and not runningSession.exited:
            deadline = time.time() + poll_wait_ms / 1000.0
            while not runningSession.exited and time.time() < deadline:
                wait_time = min(0.25, max(0, deadline - time.time()))
                await asyncio.sleep(wait_time)

        stdout, stderr = self.registry.drain_session(runningSession)
        output_parts = [strip_ansi(stdout).rstrip(), strip_ansi(stderr).rstrip()]
        output = "\n".join(p for p in output_parts if p).strip()

        exited = runningSession.exited
        if exited:
            exit_code = runningSession.exit_code if runningSession.exit_code is not None else 0
            exit_signal = runningSession.exit_signal
            status_str = (
                "completed"
                if exit_code == 0 and exit_signal is None
                else "failed"
            )
            self.registry.mark_exited(
                runningSession, runningSession.exit_code, runningSession.exit_signal, status_str
            )
            exit_label = (
                f"signal {exit_signal}" if exit_signal else f"code {exit_code}"
            )
            return ToolResult(
                success=True,
                output=(
                    (output or "(no new output)")
                    + f"\n\nProcess exited with {exit_label}."
                ),
                metadata={
                    "status": status_str,
                    "session_id": sid,
                    "exit_code": exit_code,
                },
            )

        return ToolResult(
            success=True,
            output=(output or "(no new output)") + "\n\nProcess still running.",
            metadata={"status": "running", "session_id": sid},
        )

    def _handle_log(
        self, 
        params: ProcessToolParams, 
        runningSession: Optional[ProcessSession], 
        finishedSession: Optional[FinishedSession],
    ) -> ToolResult:
        sid = params.session_id

        if not (runningSession or finishedSession):
            return ToolResult(
                success=False, output="", error=f"No session found for {sid}"
            )

        if runningSession and not runningSession.backgrounded:
            return ToolResult(
                success=False,
                output="",
                error=f"Session {sid} is not backgrounded.",
            )

        aggregated = runningSession.aggregated if runningSession else finishedSession.aggregated
        lines = aggregated.split("\n") if aggregated else []
        total_lines = len(lines)

        # Apply pagination
        offset = params.offset or 0
        limit = params.limit
        using_default_tail = offset == 0 and limit is None

        if using_default_tail:
            # Default: show last N lines
            display_lines = lines[-DEFAULT_LOG_TAIL_LINES:]
        else:
            end = offset + limit if limit else len(lines)
            display_lines = lines[offset:end]

        text = "\n".join(display_lines) or "(no output yet)"

        tail_note = ""
        if using_default_tail and total_lines > DEFAULT_LOG_TAIL_LINES:
            tail_note = (
                f"\n\n[showing last {DEFAULT_LOG_TAIL_LINES} of "
                f"{total_lines} lines; pass offset/limit to page]"
            )

        is_exited = (
            runningSession.exited if runningSession else True
        )

        return ToolResult(
            success=True,
            output=text + tail_note,
            metadata={
                "status": "completed" if is_exited else "running",
                "session_id": sid,
                "total_lines": total_lines,
                "truncated": runningSession.truncated if runningSession else finishedSession.truncated,
            },
        )

    def _handle_write(
        self,
        params: ProcessToolParams, 
        session: Optional[ProcessSession],
    ) -> ToolResult:
        result = _resolve_writable_stdin(session, params.session_id)
        if result is not None:
            return result

        data = params.data or ""
        session.stdin.write(data)  # type: ignore[union-attr]

        if params.eof:
            session.stdin.close()  # type: ignore[union-attr]

        return ToolResult(
            success=True,
            output=(
                f"Wrote {len(data)} bytes to session {params.session_id}"
                + (" (stdin closed)" if params.eof else "")
            ),
            metadata={"status": "running", "session_id": params.session_id},
        )

    def _handle_send_keys(
        self,
        params: ProcessToolParams, 
        session: Optional[ProcessSession],
    ) -> ToolResult:
        result = _resolve_writable_stdin(session, params.session_id)
        if result is not None:
            return result

        encoding = encode_key_sequence(
            KeyEncodingRequest(
                keys=params.keys,
                hex_bytes=params.hex_bytes,
                literal=params.literal,
            )
        )
        if not encoding.data:
            return ToolResult(
                success=False, output="", error="No key data provided."
            )

        session.stdin.write(encoding.data)  # type: ignore[union-attr]

        text = f"Sent {len(encoding.data)} bytes to session {params.session_id}."
        if encoding.warnings:
            text += "\nWarnings:\n- " + "\n- ".join(encoding.warnings)

        return ToolResult(
            success=True,
            output=text,
            metadata={"status": "running", "session_id": params.session_id},
        )

    def _handle_paste(
        self, 
        params: ProcessToolParams, 
        session: Optional[ProcessSession],
    ) -> ToolResult:
        result = _resolve_writable_stdin(session, params.session_id)
        if result is not None:
            return result

        payload = encode_paste(params.text or "", params.bracketed)
        if not payload:
            return ToolResult(
                success=False, output="", error="No paste text provided."
            )

        session.stdin.write(payload)  # type: ignore[union-attr]

        return ToolResult(
            success=True,
            output=f"Pasted {len(params.text or '')} chars to session {params.session_id}.",
            metadata={"status": "running", "session_id": params.session_id},
        )

    def _handle_submit(
        self, 
        params: ProcessToolParams,
        session: Optional[ProcessSession],
    ) -> ToolResult:
        result = _resolve_writable_stdin(session, params.session_id)
        if result is not None:
            return result

        session.stdin.write("\r")  # type: ignore[union-attr]

        return ToolResult(
            success=True,
            output=f"Submitted session {params.session_id} (sent CR).",
            metadata={"status": "running", "session_id": params.session_id},
        )

    def _handle_kill(
        self, 
        params: ProcessToolParams, 
        session: Optional[ProcessSession],
    ) -> ToolResult:
        sid = params.session_id
        if session is None:
            return ToolResult(
                success=False, output="", error=f"No active session found for {sid}"
            )
        if not session.backgrounded:
            return ToolResult(
                success=False, output="", error=f"Session {sid} is not backgrounded."
            )

        supervisor = get_process_supervisor()
        active_run = supervisor.get_active(sid)

        if active_run is not None:
            supervisor.cancel(sid)
            return ToolResult(
                success=True,
                output=f"Termination requested for session {sid}.",
                metadata={"status": "killed", "session_id": sid},
            )

        # Fallback: direct kill via pid
        pid = session.pid
        if pid is not None and pid > 0:
            kill_process_tree(pid)
            self.registry.mark_exited(session, None, 9, "killed")
            return ToolResult(
                success=True,
                output=f"Killed session {sid}.",
                metadata={"status": "killed", "session_id": sid},
            )

        return ToolResult(
            success=False,
            output="",
            error=f"Unable to terminate session {sid}: no active run or process ID.",
        )

# --- Helpers ---

def _resolve_writable_stdin(
    session: Optional[ProcessSession], session_id: Optional[str]
) -> Optional[ToolResult]:
    """
    Validate that the session exists, is backgrounded, and has writable stdin.
    Returns a ToolResult error if validation fails, None if all good.
    """
    if session is None:
        return ToolResult(
            success=False,
            output="",
            error=f"No active session found for {session_id}",
        )
    if not session.backgrounded:
        return ToolResult(
            success=False,
            output="",
            error=f"Session {session_id} is not backgrounded.",
        )
    if session.stdin is None:
        return ToolResult(
            success=False,
            output="",
            error=f"Session {session_id} stdin is not writable.",
        )
    return None

def _resolve_poll_wait_ms(value: Optional[int]) -> int:
    if value is None:
        return 0
    if isinstance(value, int) and 0 <= value <= MAX_POLL_WAIT_MS:
        return value
    return max(0, min(MAX_POLL_WAIT_MS, int(value)))

def _format_duration(seconds: float) -> str:
    """Format seconds into a compact human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"

def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
