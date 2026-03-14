"""
Adapter interface for process management.

Defines the Protocol-based contracts that both PTY and child process
adapters implement, plus shared dataclasses and enums for lifecycle tracking.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from enum import Enum
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

class TerminationReason(Enum):
    """Why a process run ended."""
    MANUAL_CANCEL = "manual-cancel"
    OVERALL_TIMEOUT = "overall-timeout"
    NO_OUTPUT_TIMEOUT = "no-output-timeout"
    SPAWN_ERROR = "spawn-error"
    SIGNAL = "signal"
    EXIT = "exit"

@dataclass
class WaitResult:
    """Result from awaiting a process adapter's wait()."""
    code: Optional[int] = None
    signal: Optional[int] = None

@dataclass
class RunExit:
    """Full exit information returned by ManagedRun.wait()."""
    reason: TerminationReason
    exit_code: Optional[int]
    exit_signal: Optional[int]
    duration_ms: float
    stdout: str
    stderr: str
    timed_out: bool
    no_output_timed_out: bool

class AdapterStdin(ABC):
    """Protocol for writing to a managed process's stdin."""

    @abstractmethod
    def write(self, data: str) -> None:
        """Write string data to the process stdin."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Send EOF / close the stdin stream."""
        ...

class ProcessAdapter(ABC):
    """
    Protocol that both PTY and child process adapters implement.
    """

    @property
    def pid(self) -> Optional[int]:
        """Process ID, if available."""
        ...

    @property
    def stdin(self) -> Optional[AdapterStdin]:
        """Stdin handle for writing to the process."""
        ...

    @abstractmethod
    def on_stdout(self, callback: Callable[[str], None]) -> None:
        """Register a callback for stdout data chunks."""
        ...

    @abstractmethod
    def on_stderr(self, callback: Callable[[str], None]) -> None:
        """Register a callback for stderr data chunks."""
        ...

    @abstractmethod
    async def wait(self) -> WaitResult:
        """Wait for the process to exit. Returns exit code and signal."""
        ...

    @abstractmethod
    def kill(self, sig: int = signal.SIGKILL) -> None:
        """Send a signal to the process."""
        ...
        
    @abstractmethod
    def dispose(self) -> None:
        """Clean up resources (listeners, threads, handles)."""
        ...

@dataclass
class ManagedRun:
    """
    Handle returned by the supervisor after spawning a process.

    Provides stdin access, a wait coroutine, and cancel function.
    """
    run_id: str
    pid: Optional[int]
    started_at_ms: float
    stdin: Optional[AdapterStdin]
    wait: Callable[[], Awaitable[RunExit]]
    cancel: Callable[[Optional[TerminationReason]], None]
