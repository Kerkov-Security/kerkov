"""
Windows process-tree termination.

Windows has no POSIX process groups or signal-0 liveness probe, so the
strategy differs from the POSIX backend:

1. Graceful: ask the tree to close (psutil ``terminate()`` on the process and
   its descendants, or ``taskkill /T`` when psutil is unavailable).
2. After a grace period, force: ``taskkill /F /T /PID`` tears down the whole
   tree, with a psutil ``kill()`` sweep as a backstop.

Liveness is checked with ``psutil.pid_exists`` (falling back to "assume alive"
so the force step always runs).
"""

from __future__ import annotations

import subprocess
import threading

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - exercised only without psutil
    psutil = None  # type: ignore

# taskkill is a built-in console app; hide its window and swallow output.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kill_tree(pid: int, grace_ms: int) -> None:
    """Best-effort tree termination: graceful, then forced after the grace period."""
    if grace_ms <= 0:
        _force_kill(pid)
        return

    _graceful_kill(pid)

    timer = threading.Timer(grace_ms / 1000.0, _force_kill, args=[pid])
    timer.daemon = True
    timer.start()


def _graceful_kill(pid: int) -> None:
    """Ask the process tree to terminate without forcing."""
    if psutil is not None:
        procs = _tree_processes(pid)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        if procs:
            return

    _taskkill(pid, force=False)


def _force_kill(pid: int) -> None:
    """Forcibly tear down the process tree."""
    if not _is_process_alive(pid):
        return

    # taskkill /F /T is the most reliable tree teardown on Windows.
    _taskkill(pid, force=True)

    if psutil is not None:
        for proc in _tree_processes(pid):
            try:
                proc.kill()
            except Exception:
                # Best-effort sweep: processes may already have exited or be inaccessible.
                pass


def _tree_processes(pid: int) -> list:
    """Return [process, *descendants] for pid, or [] if it is already gone."""
    if psutil is None:
        return []
    try:
        proc = psutil.Process(pid)
    except Exception:
        return []
    try:
        children = proc.children(recursive=True)
    except Exception:
        children = []
    # Kill children before the parent so re-parenting doesn't orphan them.
    return [*children, proc]


def _is_process_alive(pid: int) -> bool:
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return True
    return True


def _taskkill(pid: int, force: bool) -> None:
    args = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        args.insert(1, "/F")
    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            check=False,
        )
    except OSError:
        # Best-effort cleanup: if taskkill is unavailable or cannot be started,
        # intentionally ignore the error to avoid raising during termination.
        pass
