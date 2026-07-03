"""
Process supervisor — orchestrates spawning, timeouts, and lifecycle.

Picks the appropriate adapter (PTY or child), enforces an overall
wall-clock timeout and an optional no-output idle timeout, handles
PTY→child fallback on spawn failure, strips DSR cursor position requests
from PTY output, and returns ManagedRun handles.

The idle timeout is opt-in (the caller passes `no_output_timeout_ms`) so
modes that go quiet on purpose — daemons like openvpn, interactive
sessions that wait for the user — can leave it off without the supervisor
fighting them.
"""

from __future__ import annotations

import asyncio
import re
import time
import threading
import uuid
from typing import Callable, Optional

from src.core.logging.logger_factory import get_logger
from src.core.process.adapter_interface import (
    KillMode,
    ManagedRun,
    RunExit,
    TerminationReason,
    WaitResult,
)
from src.core.process.child_adapter import ChildAdapter
from src.core.process.pty_adapter import PtyAdapter

logger = get_logger("ProcessSupervisor")

# DSR (Device Status Report) cursor position request pattern
DSR_PATTERN = re.compile(r"\x1b\[\??6n")


def strip_dsr_requests(text: str) -> tuple[str, int]:
    """Strip DSR cursor position requests from PTY output. Returns (cleaned, count)."""
    count = len(DSR_PATTERN.findall(text))
    if count == 0:
        return text, 0
    cleaned = DSR_PATTERN.sub("", text)
    return cleaned, count


def build_cursor_position_response(row: int = 1, col: int = 1) -> str:
    """Build a CPR (Cursor Position Report) response sequence."""
    return f"\x1b[{row};{col}R"


def _is_timeout_reason(reason: TerminationReason) -> bool:
    return reason in (
        TerminationReason.OVERALL_TIMEOUT,
        TerminationReason.NO_OUTPUT_TIMEOUT,
    )


class ProcessSupervisor:
    """
    Singleton supervisor. Manages spawning processes via PTY or child
    adapters, overall + optional no-output timeout enforcement, and
    ManagedRun lifecycle.
    """

    def __init__(self):
        self._active: dict[str, ManagedRun] = {}

    async def spawn(
        self,
        *,
        command: str,
        mode: str = "pty",
        run_id: str = "",
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout_ms: Optional[int] = None,
        no_output_timeout_ms: Optional[int] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> ManagedRun:
        """
        Spawn a process via the selected adapter.

        Args:
            command: Shell command to execute.
            mode: "pty" or "child".
            run_id: Optional custom run ID (generated if empty).
            cwd: Working directory.
            env: Environment variables.
            timeout_ms: Overall timeout in milliseconds.
            no_output_timeout_ms: Kill if no output for this many ms. Pass
                None to disable (the right call for daemons that go quiet
                after startup and interactive sessions awaiting input).
            on_stdout: Callback for stdout data.
            on_stderr: Callback for stderr data.

        Returns:
            ManagedRun handle with stdin, wait(), and cancel().
        """
        if not run_id.strip():
            run_id = uuid.uuid4().hex[:8]

        logger.info(
            f"Spawning process: run_id={run_id}, requested_mode={mode}, "
            f"command='{command[:100]}', timeout_ms={timeout_ms}, "
            f"no_output_timeout_ms={no_output_timeout_ms}"
        )

        started_at_ms = time.time() * 1000

        # Mutable state shared by closures
        forced_reason: list[Optional[TerminationReason]] = [None]
        settled = [False]
        stdout_buf = []
        stderr_buf = []

        loop = asyncio.get_running_loop()
        timeout_handle: list[Optional[asyncio.TimerHandle]] = [None]
        no_output_handle: list[Optional[asyncio.TimerHandle]] = [None]

        # --- Helper closures ---

        def set_forced_reason(reason: TerminationReason) -> None:
            if forced_reason[0] is not None:
                return
            forced_reason[0] = reason

        cancel_adapter: list[Optional[Callable]] = [None]

        def request_cancel(reason: TerminationReason) -> None:
            set_forced_reason(reason)
            if cancel_adapter[0]:
                cancel_adapter[0]()

        def clear_timers() -> None:
            if timeout_handle[0] is not None:
                timeout_handle[0].cancel()
                timeout_handle[0] = None
            if no_output_handle[0] is not None:
                no_output_handle[0].cancel()
                no_output_handle[0] = None

        def _reset_no_output_timer() -> None:
            """Reset the no-output idle timer. Must run on the event loop thread."""
            if no_output_timeout_ms is None or settled[0]:
                return
            if no_output_handle[0] is not None:
                no_output_handle[0].cancel()
            no_output_handle[0] = loop.call_later(
                no_output_timeout_ms / 1000.0,
                lambda: request_cancel(TerminationReason.NO_OUTPUT_TIMEOUT),
            )

        def touch_output() -> None:
            """Reset the no-output idle timer (thread-safe from adapter callbacks)."""
            if no_output_timeout_ms is None or settled[0]:
                return
            try:
                loop.call_soon_threadsafe(_reset_no_output_timer)
            except RuntimeError:
                pass

        # --- Spawn adapter ---

        using_pty = mode == "pty"
        adapter = None
        cursor_response = build_cursor_position_response()

        if using_pty:
            try:
                adapter = await PtyAdapter.create(command, cwd=cwd, env=env)
            except Exception as e:
                logger.warning(
                    "PTY spawn failed (%s); retrying without PTY for '%s'",
                    e,
                    command,
                )
                using_pty = False

        if adapter is None:
            adapter = await ChildAdapter.create(
                command, cwd=cwd, env=env, stdin_mode="pipe-open"
            )
        resolved_stdio_mode = "pty" if using_pty else "pipe"

        # --- Wire output callbacks ---

        managed_run_ref: list[Optional[ManagedRun]] = [None]

        def on_supervisor_stdout(chunk: str) -> None:
            if using_pty:
                cleaned, dsr_count = strip_dsr_requests(chunk)
                # Auto-respond to DSR requests
                if dsr_count > 0 and managed_run_ref[0] and managed_run_ref[0].stdin:
                    for _ in range(dsr_count):
                        managed_run_ref[0].stdin.write(cursor_response)
                if on_stdout and cleaned:
                    on_stdout(cleaned)
                stdout_buf.append(cleaned)
            else:
                if on_stdout:
                    on_stdout(chunk)
                stdout_buf.append(chunk)
            touch_output()

        def on_supervisor_stderr(chunk: str) -> None:
            if on_stderr:
                on_stderr(chunk)
            stderr_buf.append(chunk)
            touch_output()

        adapter.on_stdout(on_supervisor_stdout)
        adapter.on_stderr(on_supervisor_stderr)

        # --- Set up cancel ---

        def do_cancel() -> None:
            if settled[0]:
                return
            adapter.kill(KillMode.FORCE)

        cancel_adapter[0] = do_cancel

        # --- Set up timers ---

        if timeout_ms is not None and timeout_ms > 0:
            timeout_handle[0] = loop.call_later(
                timeout_ms / 1000.0,
                lambda: request_cancel(TerminationReason.OVERALL_TIMEOUT),
            )

        if no_output_timeout_ms is not None and no_output_timeout_ms > 0:
            no_output_handle[0] = loop.call_later(
                no_output_timeout_ms / 1000.0,
                lambda: request_cancel(TerminationReason.NO_OUTPUT_TIMEOUT),
            )

        # --- Build wait coroutine ---

        async def wait_for_exit() -> RunExit:
            nonlocal settled
            try:
                result: WaitResult = await adapter.wait()
            except Exception:
                if not settled[0]:
                    settled[0] = True
                    clear_timers()
                    adapter.dispose()
                    self._active.pop(run_id, None)
                raise

            if settled[0]:
                reason = forced_reason[0] or TerminationReason.EXIT
                return RunExit(
                    reason=reason,
                    exit_code=result.code,
                    exit_signal=result.signal,
                    duration_ms=time.time() * 1000 - started_at_ms,
                    stdout="".join(stdout_buf),
                    stderr="".join(stderr_buf),
                    timed_out=_is_timeout_reason(reason),
                    no_output_timed_out=reason == TerminationReason.NO_OUTPUT_TIMEOUT,
                )

            settled[0] = True
            clear_timers()
            adapter.dispose()
            self._active.pop(run_id, None)

            if forced_reason[0] is not None:
                reason = forced_reason[0]
            elif result.signal is not None:
                reason = TerminationReason.SIGNAL
            else:
                reason = TerminationReason.EXIT

            return RunExit(
                reason=reason,
                exit_code=result.code,
                exit_signal=result.signal,
                duration_ms=time.time() * 1000 - started_at_ms,
                stdout="".join(stdout_buf),
                stderr="".join(stderr_buf),
                timed_out=_is_timeout_reason(reason),
                no_output_timed_out=reason == TerminationReason.NO_OUTPUT_TIMEOUT,
            )

        # --- Build ManagedRun ---

        def cancel_run(reason: Optional[TerminationReason] = None) -> None:
            request_cancel(reason or TerminationReason.MANUAL_CANCEL)

        managed_run = ManagedRun(
            run_id=run_id,
            pid=adapter.pid,
            started_at_ms=started_at_ms,
            stdio_mode=resolved_stdio_mode,
            stdin=adapter.stdin,
            wait=wait_for_exit,
            cancel=cancel_run,
        )
        managed_run_ref[0] = managed_run
        self._active[run_id] = managed_run

        logger.info(
            f"Process spawned: run_id={run_id}, pid={adapter.pid}, "
            f"requested_mode={mode}, resolved_stdio={resolved_stdio_mode}, "
            f"using_pty={using_pty}"
        )
        return managed_run

    def cancel(
        self,
        run_id: str,
        reason: TerminationReason = TerminationReason.MANUAL_CANCEL,
    ) -> None:
        """Cancel an active run by ID."""
        run = self._active.get(run_id)
        if run is None:
            logger.warning(f"Cancel requested for unknown run_id={run_id}")
            return
        logger.info(f"Cancelling run_id={run_id}, reason={reason.value}")
        run.cancel(reason)

    def get_active(self, run_id: str) -> Optional[ManagedRun]:
        return self._active.get(run_id)


# Module-level singleton
_supervisor: Optional[ProcessSupervisor] = None
_supervisor_lock = threading.Lock()


def get_process_supervisor() -> ProcessSupervisor:
    global _supervisor
    if _supervisor is None:
        with _supervisor_lock:
            if _supervisor is None:
                _supervisor = ProcessSupervisor()
    return _supervisor
