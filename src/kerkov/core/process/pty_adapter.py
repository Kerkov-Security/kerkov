"""
Platform shim for the PTY adapter.

Selects the ConPTY-backed Windows adapter or the ptyprocess-backed POSIX
adapter at import time and re-exports it under the platform-neutral names
``PtyAdapter`` / ``PtyAdapterStdin`` that the supervisor and process tool use.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from kerkov.core.process.windows_pty_adapter import WindowsPtyAdapter as PtyAdapter
    from kerkov.core.process.windows_pty_adapter import (
        WindowsPtyAdapterStdin as PtyAdapterStdin,
    )
else:
    from kerkov.core.process.posix_pty_adapter import PosixPtyAdapter as PtyAdapter
    from kerkov.core.process.posix_pty_adapter import (
        PosixPtyAdapterStdin as PtyAdapterStdin,
    )

__all__ = ["PtyAdapter", "PtyAdapterStdin"]
