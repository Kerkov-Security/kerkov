"""
Multi-session state management with output buffering.

Tracks running and finished process sessions, buffers output for
polling (pending buffers) and history (aggregated + tail).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.core.logging.logger_factory import get_logger

logger = get_logger("SessionRegistry")

DEFAULT_MAX_OUTPUT_CHARS = 200_000
DEFAULT_PENDING_OUTPUT_CHARS = 30_000
DEFAULT_TAIL_CHARS = 2000
DEFAULT_JOB_TTL_S = 30 * 60  # 30 minutes

@dataclass
class ProcessSession:
    """A running or recently-exited process session."""
    id: str
    command: str
    started_at: float  # time.time()
    cwd: Optional[str] = None
    pid: Optional[int] = None
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    pending_max_output_chars: int = DEFAULT_PENDING_OUTPUT_CHARS

    # Output buffers
    pending_stdout: list[str] = field(default_factory=list)
    pending_stderr: list[str] = field(default_factory=list)
    pending_stdout_chars: int = 0
    pending_stderr_chars: int = 0
    total_output_chars: int = 0
    aggregated: str = ""
    tail: str = ""
    truncated: bool = False

    # Lifecycle
    exited: bool = False
    exit_code: Optional[int] = None
    exit_signal: Optional[int] = None
    backgrounded: bool = False

    # Stdin handle (set after spawn, typed as Any to avoid circular import)
    stdin: Optional[object] = None


@dataclass
class FinishedSession:
    """Snapshot of a session after it exits."""
    id: str
    command: str
    started_at: float
    ended_at: float
    cwd: Optional[str] = None
    status: str = "completed"  # "completed" | "failed" | "killed"
    exit_code: Optional[int] = None
    exit_signal: Optional[int] = None
    aggregated: str = ""
    tail: str = ""
    truncated: bool = False
    total_output_chars: int = 0


def _trim_with_cap(text: str, max_chars: int) -> str:
    """Keep the last max_chars characters of text."""
    if len(text) <= max_chars:
        return text
    return text[len(text) - max_chars:]


def _tail(text: str, max_chars: int = DEFAULT_TAIL_CHARS) -> str:
    """Return the last max_chars characters."""
    if len(text) <= max_chars:
        return text
    return text[len(text) - max_chars:]


def _cap_pending_buffer(buffer: list[str], pending_chars: int, cap: int) -> int:
    """
    Cap a pending output buffer by dropping oldest chunks.
    Returns the new char count.
    """
    if pending_chars <= cap:
        return pending_chars

    # If last chunk alone exceeds cap, keep only tail of last chunk
    if buffer and len(buffer[-1]) >= cap:
        last = buffer[-1]
        buffer.clear()
        buffer.append(last[len(last) - cap:])
        return cap

    # Drop oldest chunks until under cap
    while buffer and pending_chars - len(buffer[0]) >= cap:
        pending_chars -= len(buffer[0])
        buffer.pop(0)

    # Trim first chunk if still over
    if buffer and pending_chars > cap:
        overflow = pending_chars - cap
        buffer[0] = buffer[0][overflow:]
        pending_chars = cap

    return pending_chars


class SessionRegistry:
    """
    Thread-safe session registry. Manages running and finished sessions
    with output buffering, draining, and automatic cleanup.
    """

    def __init__(self, job_ttl_s: float = DEFAULT_JOB_TTL_S):
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, FinishedSession] = {}
        self._lock = threading.Lock()
        self._job_ttl_s = job_ttl_s
        self._sweeper: Optional[threading.Timer] = None
        self._sweeper_interval_s = max(30.0, job_ttl_s / 6)

    def create_session_id(self) -> str:
        """Generate a unique 8-char hex slug not already in use."""
        with self._lock:
            for _ in range(100):
                slug = secrets.token_hex(4)
                if slug not in self._running and slug not in self._finished:
                    return slug
            # Extremely unlikely fallback
            return secrets.token_hex(8)

    def add_session(self, session: ProcessSession) -> None:
        logger.info(f"Session added: id={session.id}, command='{session.command[:80]}', pid={session.pid}")
        with self._lock:
            self._running[session.id] = session
        self._start_sweeper()

    def get_session(self, session_id: str) -> Optional[ProcessSession]:
        with self._lock:
            return self._running.get(session_id)

    def get_finished_session(self, session_id: str) -> Optional[FinishedSession]:
        with self._lock:
            return self._finished.get(session_id)

    def delete_session(self, session_id: str) -> None:
        logger.info(f"Session deleted: id={session_id}")
        with self._lock:
            self._running.pop(session_id, None)
            self._finished.pop(session_id, None)

    def append_output(
        self, session: ProcessSession, stream: str, chunk: str
    ) -> None:
        """
        Append an output chunk to the session's pending buffer and aggregated history.

        Thread-safe — called from PTY reader threads.
        """
        with self._lock:
            if stream == "stdout":
                buffer = session.pending_stdout
                buffer.append(chunk)
                session.pending_stdout_chars += len(chunk)
                pending_cap = min(
                    session.pending_max_output_chars, session.max_output_chars
                )
                if session.pending_stdout_chars > pending_cap:
                    session.truncated = True
                    session.pending_stdout_chars = _cap_pending_buffer(
                        buffer, session.pending_stdout_chars, pending_cap
                    )
            else:
                buffer = session.pending_stderr
                buffer.append(chunk)
                session.pending_stderr_chars += len(chunk)
                pending_cap = min(
                    session.pending_max_output_chars, session.max_output_chars
                )
                if session.pending_stderr_chars > pending_cap:
                    session.truncated = True
                    session.pending_stderr_chars = _cap_pending_buffer(
                        buffer, session.pending_stderr_chars, pending_cap
                    )

            session.total_output_chars += len(chunk)

            # Update aggregated (keep tail at max_output_chars)
            new_aggregated = _trim_with_cap(
                session.aggregated + chunk, session.max_output_chars
            )
            if len(new_aggregated) < len(session.aggregated) + len(chunk):
                session.truncated = True
            session.aggregated = new_aggregated
            session.tail = _tail(session.aggregated)

    def drain_session(self, session: ProcessSession) -> tuple[str, str]:
        """Drain and return (stdout, stderr) pending buffers. Clears them."""
        with self._lock:
            stdout = "".join(session.pending_stdout)
            stderr = "".join(session.pending_stderr)
            session.pending_stdout.clear()
            session.pending_stderr.clear()
            session.pending_stdout_chars = 0
            session.pending_stderr_chars = 0
            return stdout, stderr

    def mark_exited(
        self,
        session: ProcessSession,
        exit_code: Optional[int],
        exit_signal: Optional[int],
        status: str,
    ) -> None:
        """Mark session as exited and move to finished."""
        logger.info(
            f"Session exited: id={session.id}, status={status}, "
            f"exit_code={exit_code}, exit_signal={exit_signal}"
        )
        stdin_to_close = None
        with self._lock:
            session.exited = True
            session.exit_code = exit_code
            session.exit_signal = exit_signal
            session.tail = _tail(session.aggregated)
            stdin_to_close = self._move_to_finished(session, status)
        # Close stdin outside the lock to avoid blocking other operations
        if stdin_to_close is not None:
            try:
                stdin_to_close.close()
            except Exception:
                pass

    def mark_backgrounded(self, session: ProcessSession) -> None:
        with self._lock:
            session.backgrounded = True

    def list_running_sessions(self) -> list[ProcessSession]:
        """Return backgrounded running sessions."""
        with self._lock:
            return [s for s in self._running.values() if s.backgrounded]

    def list_finished_sessions(self) -> list[FinishedSession]:
        with self._lock:
            return list(self._finished.values())

    def _move_to_finished(self, session: ProcessSession, status: str) -> Optional[object]:
        """Move session from running to finished. Must be called under lock.

        Returns the stdin handle to close (caller closes outside the lock),
        or None if there is nothing to close.
        """
        self._running.pop(session.id, None)

        # Extract stdin for caller to close outside the lock
        stdin_to_close = None
        if session.stdin is not None and hasattr(session.stdin, "close"):
            stdin_to_close = session.stdin
        session.stdin = None

        if not session.backgrounded:
            return stdin_to_close

        self._finished[session.id] = FinishedSession(
            id=session.id,
            command=session.command,
            started_at=session.started_at,
            ended_at=time.time(),
            cwd=session.cwd,
            status=status,
            exit_code=session.exit_code,
            exit_signal=session.exit_signal,
            aggregated=session.aggregated,
            tail=session.tail,
            truncated=session.truncated,
            total_output_chars=session.total_output_chars,
        )
        return stdin_to_close

    def _reap_stale_running(self) -> None:
        """Move running sessions that have exited but weren't marked to finished."""
        stale: list[ProcessSession] = []
        with self._lock:
            stale = [s for s in self._running.values() if s.exited]
        for session in stale:
            self.mark_exited(
                session,
                session.exit_code,
                session.exit_signal,
                "completed" if session.exit_code in (None, 0) else "failed",
            )

    def _start_sweeper(self) -> None:
        with self._lock:
            if self._sweeper is not None:
                return

            def _sweep():
                self._reap_stale_running()
                self._prune_finished()
                with self._lock:
                    self._sweeper = None
                    has_sessions = bool(self._running) or bool(self._finished)
                if has_sessions:
                    self._start_sweeper()

            self._sweeper = threading.Timer(self._sweeper_interval_s, _sweep)
            self._sweeper.daemon = True
            self._sweeper.start()

    def _prune_finished(self) -> None:
        cutoff = time.time() - self._job_ttl_s
        with self._lock:
            expired = [
                sid for sid, s in self._finished.items()
                if s.ended_at < cutoff
            ]
            for sid in expired:
                del self._finished[sid]
        if expired:
            logger.info(f"Pruned {len(expired)} expired finished sessions")


# Module-level singleton
_registry: Optional[SessionRegistry] = None
_registry_lock = threading.Lock()


def get_session_registry() -> SessionRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SessionRegistry()
    return _registry
