"""
Process management system — PTY and subprocess adapters, supervisor, session registry.
"""

from src.core.process.ansi_strip import strip_ansi
from src.core.process.adapter_interface import (
    AdapterStdin,
    ManagedRun,
    ProcessAdapter,
    RunExit,
    RunState,
    TerminationReason,
    WaitResult,
)
from src.core.process.key_encoder import (
    KeyEncodingRequest,
    KeyEncodingResult,
    encode_key_sequence,
    encode_paste,
)
from src.core.process.kill_tree import kill_process_tree
from src.core.process.session_registry import (
    FinishedSession,
    ProcessSession,
    SessionRegistry,
    get_session_registry,
)
from src.core.process.supervisor import ProcessSupervisor, get_process_supervisor

__all__ = [
    "strip_ansi",
    "AdapterStdin",
    "FinishedSession",
    "KeyEncodingRequest",
    "KeyEncodingResult",
    "ManagedRun",
    "ProcessAdapter",
    "ProcessSession",
    "ProcessSupervisor",
    "RunExit",
    "RunState",
    "SessionRegistry",
    "TerminationReason",
    "WaitResult",
    "encode_key_sequence",
    "encode_paste",
    "get_process_supervisor",
    "get_session_registry",
    "kill_process_tree",
]
