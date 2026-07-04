"""
Process management system — PTY and subprocess adapters, supervisor, session registry.
"""

from kerkov.core.process.adapter_interface import (
    AdapterStdin,
    KillMode,
    ManagedRun,
    ProcessAdapter,
    RunExit,
    TerminationReason,
    WaitResult,
)
from kerkov.core.process.ansi_strip import strip_ansi
from kerkov.core.process.key_encoder import (
    KeyEncodingRequest,
    KeyEncodingResult,
    encode_key_sequence,
    encode_paste,
)
from kerkov.core.process.kill_tree import kill_process_tree
from kerkov.core.process.session_registry import (
    FinishedSession,
    ProcessSession,
    SessionRegistry,
    get_session_registry,
)
from kerkov.core.process.shell_wrap import shell_wrap
from kerkov.core.process.supervisor import ProcessSupervisor, get_process_supervisor

__all__ = [
    "strip_ansi",
    "AdapterStdin",
    "FinishedSession",
    "KeyEncodingRequest",
    "KeyEncodingResult",
    "KillMode",
    "ManagedRun",
    "ProcessAdapter",
    "ProcessSession",
    "ProcessSupervisor",
    "RunExit",
    "SessionRegistry",
    "TerminationReason",
    "WaitResult",
    "encode_key_sequence",
    "encode_paste",
    "get_process_supervisor",
    "get_session_registry",
    "kill_process_tree",
    "shell_wrap",
]
