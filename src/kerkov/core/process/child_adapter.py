"""
Child process adapter using asyncio subprocess.

Fallback adapter for when PTY is not needed or unavailable. Spawns with piped
stdin/stdout/stderr through the platform shell (``shell_wrap``). On POSIX the
child gets its own session (``start_new_session``); on Windows it gets a new
process group (``CREATE_NEW_PROCESS_GROUP``) so the tree can be torn down and a
CTRL_BREAK delivered.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from typing import Any, Callable, Optional

from kerkov.core.process.adapter_interface import (
    AdapterStdin,
    KillMode,
    ProcessAdapter,
    WaitResult,
)
from kerkov.core.process.kill_tree import kill_process_tree
from kerkov.core.process.shell_wrap import resolve_shell, shell_wrap


def _spawn_kwargs() -> dict[str, Any]:
    """Platform flags that detach the child for group/tree termination."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _create_subprocess(
    argv: list[str],
    command: str,
    shell: Optional[str],
    spawn_kwargs: dict[str, Any],
) -> asyncio.subprocess.Process:
    """
    Spawn the child, preserving shell quoting on every platform.

    Windows ``cmd`` needs a verbatim command line (``create_subprocess_shell``
    runs ``cmd /c <command>`` without re-quoting); POSIX and Windows PowerShell
    use the argv form.
    """
    if sys.platform == "win32" and resolve_shell(shell) == "cmd":
        return await asyncio.create_subprocess_shell(command, **spawn_kwargs)
    return await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)


class ChildAdapterStdin(AdapterStdin):
    """Wraps asyncio.subprocess stdin StreamWriter to implement AdapterStdin."""

    def __init__(self, writer: asyncio.StreamWriter):
        self._writer = writer
        self._closed = False

    def write(self, data: str) -> None:
        if self._closed:
            return
        try:
            self._writer.write(data.encode("utf-8"))
        except (OSError, ConnectionResetError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except (OSError, ConnectionResetError):
            pass


class ChildAdapter(ProcessAdapter):
    """
    Implements ProcessAdapter using asyncio subprocess with piped stdio.

    Spawned detached so the whole tree can be killed: a new session on POSIX,
    a new process group on Windows.
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._stdin_wrapper: Optional[ChildAdapterStdin] = None
        if proc.stdin is not None:
            self._stdin_wrapper = ChildAdapterStdin(proc.stdin)
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._disposed = False

    @staticmethod
    async def create(
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin_mode: str = "pipe-closed",
        input_data: Optional[str] = None,
        shell: Optional[str] = None,
    ) -> ChildAdapter:
        """
        Factory. Spawns the command through the platform shell with piped
        stdout/stderr.

        stdin_mode:
          - "pipe-open": leave stdin open for writing
          - "pipe-closed": close stdin immediately (or after writing input_data)
        """
        argv = shell_wrap(command, shell)
        spawn_kwargs: dict[str, Any] = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            **_spawn_kwargs(),
        )
        proc = await _create_subprocess(argv, command, shell, spawn_kwargs)

        adapter = ChildAdapter(proc)

        # Write initial input data if provided
        if input_data is not None and proc.stdin is not None:
            try:
                proc.stdin.write(input_data.encode("utf-8"))
                await proc.stdin.drain()
            except (OSError, ConnectionResetError):
                pass

        # Close stdin if mode is pipe-closed
        if stdin_mode == "pipe-closed" and proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ConnectionResetError):
                pass

        return adapter

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid

    @property
    def stdin(self) -> Optional[ChildAdapterStdin]:
        return self._stdin_wrapper

    def on_stdout(self, callback: Callable[[str], None]) -> None:
        """Start an asyncio task reading stdout chunks and calling callback."""
        if self._proc.stdout is None:
            return

        async def _reader():
            stream = self._proc.stdout
            try:
                while True:
                    chunk = await stream.read(4096)  # type: ignore[union-attr]
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    callback(text)
            except (asyncio.CancelledError, OSError):
                pass

        self._stdout_task = asyncio.create_task(_reader())

    def on_stderr(self, callback: Callable[[str], None]) -> None:
        """Start an asyncio task reading stderr chunks and calling callback."""
        if self._proc.stderr is None:
            return

        async def _reader():
            stream = self._proc.stderr
            try:
                while True:
                    chunk = await stream.read(4096)  # type: ignore[union-attr]
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    callback(text)
            except (asyncio.CancelledError, OSError):
                pass

        self._stderr_task = asyncio.create_task(_reader())

    async def wait(self) -> WaitResult:
        """Wait for the process to exit. Returns code and signal."""
        returncode = await self._proc.wait()
        if returncode < 0:
            # Negative returncode means killed by signal (POSIX only)
            return WaitResult(code=None, signal=-returncode)
        return WaitResult(code=returncode, signal=None)

    def kill(self, mode: KillMode = KillMode.FORCE) -> None:
        """Terminate the process. FORCE kills the tree; GRACEFUL asks it to stop."""
        pid = self._proc.pid
        if mode == KillMode.FORCE and pid is not None:
            kill_process_tree(pid)
            return
        try:
            self._proc.send_signal(_graceful_signal())
        except (OSError, ProcessLookupError, ValueError):
            # Best-effort graceful stop: the process may have already exited,
            # the PID may no longer be valid, or the platform may reject signal
            # delivery in the current process state.
            pass

    def dispose(self) -> None:
        """Cancel reader tasks and clean up."""
        if self._disposed:
            return
        self._disposed = True
        if self._stdout_task and not self._stdout_task.done():
            self._stdout_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stdout_task = None
        self._stderr_task = None


def _graceful_signal() -> int:
    """The signal used for a polite stop on this platform."""
    if sys.platform == "win32":
        return signal.CTRL_BREAK_EVENT  # type: ignore[attr-defined]
    return signal.SIGTERM
