"""
PTY-based process adapter using ptyprocess.

Spawns commands in a real pseudo-terminal so interactive programs
(Python REPL, vim, htop, etc.) behave correctly. Provides a unified
output stream (PTY merges stdout/stderr).

Port of openclaw's supervisor/adapters/pty.ts.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from typing import Callable, Optional

from ptyprocess import PtyProcess

from src.core.process.adapter_interface import AdapterStdin, WaitResult
from src.core.process.kill_tree import kill_process_tree

FORCE_KILL_WAIT_FALLBACK_S = 4.0


class PtyAdapterStdin:
    """Wraps PtyProcess.write() to implement AdapterStdin protocol."""

    def __init__(self, pty_proc: PtyProcess):
        self._pty = pty_proc
        self._closed = False

    def write(self, data: str) -> None:
        if self._closed:
            return
        try:
            self._pty.write(data.encode("utf-8"))
        except (OSError, EOFError):
            pass

    def close(self) -> None:
        """Send EOF (Ctrl-D) to the PTY."""
        if self._closed:
            return
        self._closed = True
        try:
            self._pty.write(b"\x04")
        except (OSError, EOFError):
            pass


class PtyAdapter:
    """
    Implements ProcessAdapter using ptyprocess.PtyProcess.

    Unified output stream — on_stderr is a no-op since PTY merges streams.
    """

    def __init__(self, pty_proc: PtyProcess):
        self._pty = pty_proc
        self._stdin_wrapper = PtyAdapterStdin(pty_proc)
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._disposed = False

        # Wait state
        self._wait_result: Optional[WaitResult] = None
        self._wait_event = asyncio.Event()
        self._wait_lock = threading.Lock()
        self._force_kill_timer: Optional[threading.Timer] = None

    @staticmethod
    async def create(
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        cols: int = 120,
        rows: int = 30,
    ) -> PtyAdapter:
        """
        Factory method. Spawns a PTY process via shell wrapping:
          bash -c "command"
        """
        spawn_env = env if env else None
        spawn_cwd = cwd if cwd else None

        # ptyprocess.PtyProcess.spawn is blocking, run in executor
        def _spawn():
            return PtyProcess.spawn(
                ["bash", "-c", command],
                cwd=spawn_cwd,
                env=spawn_env,
                dimensions=(rows, cols),
            )

        loop = asyncio.get_event_loop()
        pty_proc = await loop.run_in_executor(None, _spawn)
        adapter = PtyAdapter(pty_proc)

        # Set up the exit watcher immediately
        adapter._start_exit_watcher()

        return adapter

    @property
    def pid(self) -> Optional[int]:
        try:
            return self._pty.pid
        except Exception:
            return None

    @property
    def stdin(self) -> Optional[PtyAdapterStdin]:
        return self._stdin_wrapper

    def on_stdout(self, callback: Callable[[str], None]) -> None:
        """Start a daemon reader thread that reads from PTY fd and dispatches chunks."""
        if self._reader_thread is not None:
            return

        def _reader():
            while not self._reader_stop.is_set():
                try:
                    data = self._pty.read(4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                    callback(text)
                except EOFError:
                    break
                except OSError:
                    break
                except Exception:
                    break

        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()

    def on_stderr(self, callback: Callable[[str], None]) -> None:
        """No-op. PTY gives a unified output stream."""
        pass

    async def wait(self) -> WaitResult:
        """Wait for the PTY process to exit."""
        if self._wait_result is not None:
            return self._wait_result

        await self._wait_event.wait()
        return self._wait_result  # type: ignore[return-value]

    def kill(self, sig: int = signal.SIGKILL) -> None:
        """Kill the PTY process. Uses tree kill for SIGKILL."""
        try:
            pid = self._pty.pid
            if sig == signal.SIGKILL and isinstance(pid, int) and pid > 0:
                kill_process_tree(pid)
            else:
                os.kill(pid, sig)
        except (OSError, ProcessLookupError):
            pass

        if sig == signal.SIGKILL:
            self._schedule_force_kill_fallback()

    def dispose(self) -> None:
        """Clean up reader thread and settle any pending wait."""
        if self._disposed:
            return
        self._disposed = True

        # Stop reader
        self._reader_stop.set()

        # Cancel force-kill timer
        self._cancel_force_kill_timer()

        # Settle wait if still pending
        self._settle_wait(WaitResult(code=None, signal=None))

    def _start_exit_watcher(self) -> None:
        """Start a daemon thread that waits for PTY exit and settles the wait future."""
        def _watcher():
            try:
                # PtyProcess.wait() blocks until exit
                status = self._pty.wait()
            except Exception:
                self._settle_wait(WaitResult(code=None, signal=None))
                return

            # Decode wait status
            if os.WIFSIGNALED(status):
                sig = os.WTERMSIG(status)
                self._settle_wait(WaitResult(code=None, signal=sig))
            elif os.WIFEXITED(status):
                code = os.WEXITSTATUS(status)
                self._settle_wait(WaitResult(code=code, signal=None))
            else:
                self._settle_wait(WaitResult(code=None, signal=None))

        t = threading.Thread(target=_watcher, daemon=True)
        t.start()

    def _settle_wait(self, result: WaitResult) -> None:
        """Thread-safe: set the wait result and signal the asyncio event."""
        with self._wait_lock:
            if self._wait_result is not None:
                return
            self._wait_result = result
            self._cancel_force_kill_timer()

        # Signal the asyncio event from whatever thread we're on
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(self._wait_event.set)
            else:
                self._wait_event.set()
        except RuntimeError:
            # No running loop — set directly (will be picked up when loop resumes)
            self._wait_event.set()

    def _schedule_force_kill_fallback(self) -> None:
        """
        Schedule a fallback to settle wait if PTY never emits exit.
        Some PTY hosts fail to emit onExit after kill.
        """
        self._cancel_force_kill_timer()
        self._force_kill_timer = threading.Timer(
            FORCE_KILL_WAIT_FALLBACK_S,
            lambda: self._settle_wait(WaitResult(code=None, signal=signal.SIGKILL)),
        )
        self._force_kill_timer.daemon = True
        self._force_kill_timer.start()

    def _cancel_force_kill_timer(self) -> None:
        if self._force_kill_timer is not None:
            self._force_kill_timer.cancel()
            self._force_kill_timer = None
