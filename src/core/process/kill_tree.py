"""
Cross-platform process-tree termination.

Dispatches to a platform backend (``posix_kill`` / ``windows_kill``) for the
actual teardown, while owning the shared grace-period normalization. Both
backends follow the same contract: a graceful shutdown attempt, then a forced
kill of the whole tree after the grace period.
"""

from __future__ import annotations

import math
import sys
from typing import Optional

if sys.platform == "win32":
    from src.core.process import windows_kill as _backend
else:
    from src.core.process import posix_kill as _backend

DEFAULT_GRACE_MS = 3000
MAX_GRACE_MS = 60_000



def kill_process_tree(pid: int, grace_ms: int = DEFAULT_GRACE_MS) -> None:
    """
    Best-effort process-tree termination with graceful shutdown.

    Sends a graceful stop, then forces termination of the whole tree after the
    (clamped) grace period. No-op for invalid pids.
    """
    if not isinstance(pid, int) or pid <= 0:
        return

    grace = _normalize_grace_ms(grace_ms)

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
    if grace <= 0:
        _force_kill(pid)
        return

    timer = threading.Timer(grace / 1000.0, _force_kill, args=[pid])
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

def _normalize_grace_ms(value: Optional[int]) -> int:
    if not isinstance(value, (int, float)) or math.isnan(value):
        return DEFAULT_GRACE_MS
    return max(0, min(MAX_GRACE_MS, int(value)))
