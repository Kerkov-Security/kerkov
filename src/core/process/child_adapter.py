"""
Child process adapter using asyncio subprocess.

Fallback adapter for when PTY is not needed or unavailable.
Spawns with piped stdin/stdout/stderr and detached process group.

Port of openclaw's supervisor/adapters/child.ts.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Callable, Optional

from src.core.process.adapter_interface import AdapterStdin, ProcessAdapter, WaitResult
from src.core.process.kill_tree import kill_process_tree


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

    Spawns detached (start_new_session=True) on Linux for process group kill.
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
    ) -> ChildAdapter:
        """
        Factory. Spawns: bash -c "command" with piped stdout/stderr.

        stdin_mode:
          - "pipe-open": leave stdin open for writing
          - "pipe-closed": close stdin immediately (or after writing input_data)
        """
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env=env,
        )

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
            # Negative returncode means killed by signal
            return WaitResult(code=None, signal=-returncode)
        return WaitResult(code=returncode, signal=None)

    def kill(self, sig: int = signal.SIGKILL) -> None:
        """Kill the process. Uses tree kill for SIGKILL."""
        pid = self._proc.pid
        if sig == signal.SIGKILL and pid is not None:
            kill_process_tree(pid)
        else:
            try:
                self._proc.send_signal(sig)
            except (OSError, ProcessLookupError):
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
