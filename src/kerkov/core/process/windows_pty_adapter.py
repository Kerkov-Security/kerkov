"""
Windows PTY-based process adapter using pywinpty (ConPTY).

Mirrors the POSIX ptyprocess adapter but talks to ConPTY through
``winpty.PtyProcess``. Requires Windows 10 1809+ and, for full xterm
key/DSR/bracketed-paste fidelity, Windows Terminal or another VT-enabled host
(classic conhost.exe is more limited — see docs/internal-dev).

pywinpty differs from ptyprocess in two ways the adapter has to absorb:
  * ``read()`` returns ``str`` and ``write()`` takes ``str`` (no bytes).
  * there is no ``signalstatus`` — Windows reports only an exit code.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Callable, Optional

from winpty import PTY, PtyProcess  # type: ignore

from kerkov.core.process.adapter_interface import (
    AdapterStdin,
    KillMode,
    ProcessAdapter,
    WaitResult,
)
from kerkov.core.process.kill_tree import kill_process_tree
from kerkov.core.process.shell_wrap import windows_pty_spec

FORCE_KILL_WAIT_FALLBACK_S = 4.0


def _format_env(env: dict[str, str]) -> str:
    """Encode an env mapping the way pywinpty's PTY.spawn expects (NUL-joined)."""
    return "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"


class WindowsPtyAdapterStdin(AdapterStdin):
    """Wraps winpty.PtyProcess.write() to implement AdapterStdin protocol."""

    @property
    def bracketed_paste_enabled(self) -> bool:
        return self._bracketed_paste_enabled

    def __init__(self, pty_proc: PtyProcess):
        self._pty = pty_proc
        self._closed = False
        self._bracketed_paste_enabled = False

    def write(self, data: str) -> None:
        if self._closed:
            return
        try:
            self._pty.write(data)
        except (OSError, EOFError):
            # The PTY may already be closing/closed (process exit race); writes are best-effort.
            return

    def close(self) -> None:
        """Send EOF (Ctrl-Z on Windows) to the PTY."""
        if self._closed:
            return
        self._closed = True
        try:
            self._pty.sendeof()
        except Exception:
            # Closing is best-effort; the PTY may already be gone during shutdown.
            pass


class WindowsPtyAdapter(ProcessAdapter):
    """
    Implements ProcessAdapter using winpty.PtyProcess (ConPTY).

    Unified output stream — on_stderr is a no-op since the PTY merges streams.
    """

    def __init__(self, pty_proc: PtyProcess, loop: asyncio.AbstractEventLoop):
        self._pty = pty_proc
        self._loop = loop
        self._stdin_wrapper = WindowsPtyAdapterStdin(pty_proc)
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
        shell: Optional[str] = None,
    ) -> WindowsPtyAdapter:
        """
        Factory method. Spawns a ConPTY process via the platform shell
        (cmd /c by default, or PowerShell per WINDOWS_DEFAULT_SHELL).

        Uses pywinpty's low-level ``PTY.spawn`` with a verbatim command line so
        embedded quotes survive (the argv path re-quotes via list2cmdline,
        which ``cmd`` mis-parses).
        """
        appname, cmdline = windows_pty_spec(command, shell)
        spawn_cwd = cwd if cwd else os.getcwd()
        env_str = _format_env(env) if env is not None else None

        def _spawn():
            pty = PTY(cols, rows)
            pty.spawn(appname, cmdline=cmdline, cwd=spawn_cwd, env=env_str)
            return PtyProcess(pty)

        loop = asyncio.get_running_loop()
        pty_proc = await loop.run_in_executor(None, _spawn)
        adapter = WindowsPtyAdapter(pty_proc, loop)

        adapter._start_exit_watcher()

        return adapter

    @property
    def pid(self) -> Optional[int]:
        try:
            return self._pty.pid
        except Exception:
            return None

    @property
    def stdin(self) -> Optional[WindowsPtyAdapterStdin]:
        return self._stdin_wrapper

    def on_stdout(self, callback: Callable[[str], None]) -> None:
        """Start a daemon reader thread that reads from the PTY and dispatches chunks."""
        if self._reader_thread is not None:
            return

        def _reader():
            while not self._reader_stop.is_set():
                try:
                    data = self._pty.read(4096)
                    if not data:
                        break
                    text = (
                        data.decode("utf-8", errors="replace")
                        if isinstance(data, bytes)
                        else data
                    )
                    if "\x1b[?2004h" in text:
                        self._stdin_wrapper._bracketed_paste_enabled = True
                    if "\x1b[?2004l" in text:
                        self._stdin_wrapper._bracketed_paste_enabled = False
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

    def kill(self, mode: KillMode = KillMode.FORCE) -> None:
        """Terminate the ConPTY process. FORCE tears down the whole tree."""
        pid = self.pid
        try:
            if mode == KillMode.FORCE and isinstance(pid, int) and pid > 0:
                kill_process_tree(pid)
            else:
                self._pty.terminate(force=False)
        except Exception:
            # Termination is best-effort; FORCE mode still schedules the fallback below.
            pass

        if mode == KillMode.FORCE:
            try:
                self._pty.terminate(force=True)
            except Exception:
                # Force termination can fail if the process already exited.
                # The fallback timer still settles wait() for this race.
                pass
            self._schedule_force_kill_fallback()

    def dispose(self) -> None:
        """Clean up reader thread and settle any pending wait."""
        if self._disposed:
            return
        self._disposed = True
        self._reader_stop.set()
        self._cancel_force_kill_timer()
        self._settle_wait(WaitResult(code=None, signal=None))

    def _start_exit_watcher(self) -> None:
        """Start a daemon thread that waits for PTY exit and settles the wait future."""

        def _watcher():
            try:
                self._pty.wait()
            except Exception:
                self._settle_wait(WaitResult(code=None, signal=None))
                return

            # winpty exposes only an exit code (no signal concept on Windows).
            code = getattr(self._pty, "exitstatus", None)
            self._settle_wait(WaitResult(code=code, signal=None))

        t = threading.Thread(target=_watcher, daemon=True)
        t.start()

    def _settle_wait(self, result: WaitResult) -> None:
        """Thread-safe: set the wait result and signal the asyncio event."""
        with self._wait_lock:
            if self._wait_result is not None:
                return
            self._wait_result = result
            self._cancel_force_kill_timer()

        try:
            self._loop.call_soon_threadsafe(self._wait_event.set)
        except RuntimeError:
            self._wait_event.set()

    def _schedule_force_kill_fallback(self) -> None:
        """Settle the wait if the PTY never reports exit after a forced kill."""
        with self._wait_lock:
            if self._wait_result is not None:
                return
            self._cancel_force_kill_timer()
            self._force_kill_timer = threading.Timer(
                FORCE_KILL_WAIT_FALLBACK_S,
                lambda: self._settle_wait(WaitResult(code=None, signal=None)),
            )
            self._force_kill_timer.daemon = True
            self._force_kill_timer.start()

    def _cancel_force_kill_timer(self) -> None:
        if self._force_kill_timer is not None:
            self._force_kill_timer.cancel()
            self._force_kill_timer = None
