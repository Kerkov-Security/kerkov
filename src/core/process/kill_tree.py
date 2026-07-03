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

    _backend.kill_tree(pid, _normalize_grace_ms(grace_ms))


def _normalize_grace_ms(value: Optional[int]) -> int:
    if not isinstance(value, (int, float)) or math.isnan(value):
        return DEFAULT_GRACE_MS
    return max(0, min(MAX_GRACE_MS, int(value)))
