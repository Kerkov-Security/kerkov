"""Subprocess support for asyncio event loops that lack a subprocess transport.

Daphne runs the web console on Twisted's asyncio reactor, which requires a
selector loop. On Windows that loop has no subprocess transport, so
``asyncio.create_subprocess_exec`` raises ``NotImplementedError`` for Playwright
and every Kerkov caller. ``ensure_subprocess_support`` gives such a loop a
transport backed by ``subprocess.Popen`` with one thread per pipe and one exit
watcher, keeping ``asyncio.subprocess.Process`` semantics for callers. Loops that
already support subprocesses (POSIX, Windows Proactor) are left untouched.
"""

from __future__ import annotations

import asyncio
import queue
import subprocess
import threading
import types
from asyncio import base_subprocess
from typing import Any, Callable, Optional

READ_CHUNK_BYTES = 64 * 1024
WRITE_HIGH_WATER_BYTES = 64 * 1024
WRITE_LOW_WATER_BYTES = 16 * 1024

_UNIMPLEMENTED_TRANSPORT = asyncio.BaseEventLoop.__dict__["_make_subprocess_transport"]


def loop_supports_subprocesses(loop: asyncio.AbstractEventLoop) -> bool:
    """Return False when ``loop`` keeps asyncio's unimplemented subprocess stub.

    Windows selector loops, such as the one Daphne's Twisted reactor runs on, have
    no subprocess transport, so ``asyncio.create_subprocess_exec`` raises
    ``NotImplementedError`` there.
    """
    if not isinstance(loop, asyncio.BaseEventLoop):
        return True
    make_transport = getattr(loop, "_make_subprocess_transport", None)
    return getattr(make_transport, "__func__", None) is not _UNIMPLEMENTED_TRANSPORT


def ensure_subprocess_support(loop: asyncio.AbstractEventLoop) -> bool:
    """Install the thread-backed transport on ``loop`` if it has none; report it."""
    if loop_supports_subprocesses(loop):
        return False
    # The asyncio subprocess entry points all go through this loop hook.
    loop._make_subprocess_transport = types.MethodType(  # type: ignore[attr-defined]
        _make_threaded_subprocess_transport, loop
    )
    return True


async def _make_threaded_subprocess_transport(
    loop: asyncio.BaseEventLoop,
    protocol: asyncio.SubprocessProtocol,
    args: Any,
    shell: bool,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    bufsize: int,
    extra: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> _ThreadedSubprocessTransport:
    waiter = loop.create_future()
    transport = _ThreadedSubprocessTransport(
        loop,
        protocol,
        args,
        shell,
        stdin,
        stdout,
        stderr,
        bufsize,
        waiter=waiter,
        extra=extra,
        **kwargs,
    )
    try:
        await waiter
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        transport.close()
        await transport._wait()
        raise
    return transport


def _post(
    loop: asyncio.AbstractEventLoop, callback: Callable[..., Any], *args: Any
) -> None:
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        pass  # The loop closed; nobody is left to notify.


class _ThreadedSubprocessTransport(base_subprocess.BaseSubprocessTransport):
    """Asyncio's base subprocess transport over ``Popen`` and helper threads."""

    def _start(
        self,
        args: Any,
        shell: bool,
        stdin: Any,
        stdout: Any,
        stderr: Any,
        bufsize: int,
        **kwargs: Any,
    ) -> None:
        self._proc = proc = subprocess.Popen(
            args,
            shell=shell,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            bufsize=bufsize,
            **kwargs,
        )
        threading.Thread(
            target=self._watch_exit,
            args=(self._loop, proc),
            name=f"kerkov-subprocess-{proc.pid}-exit",
            daemon=True,
        ).start()

    def _watch_exit(
        self, loop: asyncio.AbstractEventLoop, proc: subprocess.Popen
    ) -> None:
        _post(loop, self._process_exited, proc.wait())

    async def _connect_pipes(self, waiter: Optional[asyncio.Future[None]]) -> None:
        # Mirrors BaseSubprocessTransport._connect_pipes with thread-backed pipes.
        try:
            proc = self._proc
            assert proc is not None
            if proc.stdin is not None:
                write_proto = base_subprocess.WriteSubprocessPipeProto(self, 0)
                _ThreadedWritePipe(self._loop, proc.stdin, write_proto, proc.pid)
                self._pipes[0] = write_proto  # type: ignore[assignment]
            for fd, pipe in ((1, proc.stdout), (2, proc.stderr)):
                if pipe is not None:
                    read_proto = base_subprocess.ReadSubprocessPipeProto(self, fd)
                    _ThreadedReadPipe(self._loop, pipe, read_proto, proc.pid)
                    self._pipes[fd] = read_proto  # type: ignore[assignment]

            self._loop.call_soon(self._protocol.connection_made, self)
            for callback, data in self._pending_calls:
                self._loop.call_soon(callback, *data)
            self._pending_calls = None  # type: ignore[assignment]
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if waiter is not None and not waiter.cancelled():
                waiter.set_exception(exc)
        else:
            if waiter is not None and not waiter.cancelled():
                waiter.set_result(None)


class _ThreadedReadPipe(asyncio.ReadTransport):
    """Read a child's pipe on a thread; only that thread touches the handle."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        pipe: Any,
        protocol: asyncio.Protocol,
        pid: int,
    ) -> None:
        super().__init__({"pipe": pipe})
        self._loop = loop
        self._pipe = pipe
        self._protocol: Optional[asyncio.Protocol] = protocol
        self._closing = False
        self._reading = threading.Event()
        self._reading.set()
        protocol.connection_made(self)
        threading.Thread(
            target=self._read_loop, name=f"kerkov-subprocess-{pid}-read", daemon=True
        ).start()

    def _read_loop(self) -> None:
        error: Optional[Exception] = None
        try:
            while True:
                self._reading.wait()
                if self._closing:
                    break
                data = self._pipe.read(READ_CHUNK_BYTES)
                if not data:
                    break
                _post(self._loop, self._deliver, data)
        except (OSError, ValueError) as exc:
            error = exc
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass
            _post(self._loop, self._lose_connection, error)

    def _deliver(self, data: bytes) -> None:
        if not self._closing and self._protocol is not None:
            self._protocol.data_received(data)

    def _lose_connection(self, error: Optional[Exception]) -> None:
        protocol, self._protocol = self._protocol, None
        if protocol is not None:
            protocol.connection_lost(None if self._closing else error)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        # A blocked read cannot be interrupted, so report the loss now and let
        # the thread close the handle when the child closes its end.
        if self._closing:
            return
        self._closing = True
        self._reading.set()
        self._loop.call_soon(self._lose_connection, None)

    def is_reading(self) -> bool:
        return not self._closing and self._reading.is_set()

    def pause_reading(self) -> None:
        self._reading.clear()

    def resume_reading(self) -> None:
        self._reading.set()

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self._protocol = protocol  # type: ignore[assignment]

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol  # type: ignore[return-value]


class _ThreadedWritePipe(asyncio.WriteTransport):
    """Write to a child's pipe on a thread, with asyncio flow control."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        pipe: Any,
        protocol: asyncio.BaseProtocol,
        pid: int,
    ) -> None:
        super().__init__({"pipe": pipe})
        self._loop = loop
        self._pipe = pipe
        self._protocol: Optional[asyncio.BaseProtocol] = protocol
        self._queue: queue.SimpleQueue[Optional[bytes]] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._buffered = 0
        self._closing = False
        self._aborted = False
        self._paused = False
        self._high_water = WRITE_HIGH_WATER_BYTES
        self._low_water = WRITE_LOW_WATER_BYTES
        protocol.connection_made(self)
        threading.Thread(
            target=self._write_loop, name=f"kerkov-subprocess-{pid}-write", daemon=True
        ).start()

    def _write_loop(self) -> None:
        error: Optional[Exception] = None
        try:
            while (data := self._queue.get()) is not None and not self._aborted:
                view = memoryview(data)
                while view:
                    view = view[self._pipe.write(view) :]
                with self._lock:
                    self._buffered -= len(data)
                    drained = self._buffered <= self._low_water
                if drained:
                    _post(self._loop, self._maybe_resume)
        except (OSError, ValueError) as exc:
            error = exc
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass
            _post(self._loop, self._lose_connection, error)

    def _maybe_resume(self) -> None:
        if self._paused and self.get_write_buffer_size() <= self._low_water:
            self._paused = False
            if self._protocol is not None:
                self._protocol.resume_writing()

    def _lose_connection(self, error: Optional[Exception]) -> None:
        protocol, self._protocol = self._protocol, None
        if protocol is not None:
            protocol.connection_lost(error)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closing or not data:
            return
        data = bytes(data)
        with self._lock:
            self._buffered += len(data)
            buffered = self._buffered
        self._queue.put(data)
        if not self._paused and buffered > self._high_water:
            self._paused = True
            if self._protocol is not None:
                self._protocol.pause_writing()

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.close()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._queue.put(None)

    def abort(self) -> None:
        # A blocked write cannot be interrupted; drop the rest and report now.
        self._aborted = True
        self.close()
        self._loop.call_soon(self._lose_connection, None)

    def get_write_buffer_size(self) -> int:
        with self._lock:
            return self._buffered

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return self._low_water, self._high_water

    def set_write_buffer_limits(
        self, high: Optional[int] = None, low: Optional[int] = None
    ) -> None:
        self._high_water = WRITE_HIGH_WATER_BYTES if high is None else high
        self._low_water = self._high_water // 4 if low is None else low

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self._protocol = protocol

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol  # type: ignore[return-value]
