"""
POSIX process-tree termination.

Sends SIGTERM to the process group, waits a grace period, then SIGKILL
if the process is still alive. Uses a daemon timer thread so the caller
is not blocked.
"""

from __future__ import annotations

import os
import signal
import threading


def kill_tree(pid: int, grace_ms: int) -> None:
    """
    Best-effort process-tree termination with graceful shutdown.

    1. SIGTERM to process group (-pid), falling back to direct pid.
    2. After grace period, SIGKILL if still alive.
    """
    # Step 1: Try graceful SIGTERM to process group
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # Already gone
    except PermissionError:
        # No permission to signal group — try direct
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return

    # Step 2: Schedule SIGKILL after grace period (non-blocking)
    if grace_ms <= 0:
        _force_kill(pid)
        return

    timer = threading.Timer(grace_ms / 1000.0, _force_kill, args=[pid])
    timer.daemon = True
    timer.start()


def _force_kill(pid: int) -> None:
    """SIGKILL the process group, falling back to direct pid."""
    # Try process group first
    if _is_process_alive_pg(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except OSError:
            pass

    # Fall back to direct pid
    if not _is_process_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _is_process_alive(pid: int) -> bool:
    """Check if a process exists via signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_process_alive_pg(pid: int) -> bool:
    """Check if a process group exists via signal 0."""
    try:
        os.killpg(pid, 0)
        return True
    except OSError:
        return False
