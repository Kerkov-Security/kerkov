"""Live agent event model + bus for the agent UI (issue #148, Stage 0/1).

`QuietSink` already buffers everything an agent would have printed. This
module adds a thin, structured *event* layer on top so a live UI can render
one row per running agent without the agent's chatter ever entering the
master's context. The buffer stays the source of truth; events are a
fire-and-forget notification that a row should be redrawn.

The bus is deliberately minimal: a synchronous callback registry keyed by
`agent_run_id`. It is called from inside the agent's async loop (same event
loop as the master), so callbacks must be cheap and non-blocking — the live
view just mutates in-memory row state and lets Rich's `Live` repaint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class AgentEventKind(str, Enum):
    """The kinds of live events an agent emits during a run."""

    STARTED = "started"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TURN_END = "turn_end"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FINISHED = "finished"


@dataclass
class AgentEvent:
    """One live event from a running agent.

    Carries only what a status row needs; the full transcript lives in the
    agent's `QuietSink.buffer` / persisted engagement, not here.
    """

    agent_run_id: str
    kind: AgentEventKind
    agent_type: str = ""
    detail: str = ""
    monotonic_ts: float = field(default_factory=time.monotonic)


# A live listener is any callable that accepts an AgentEvent. Kept sync on
# purpose — see the module docstring.
AgentEventListener = Callable[[AgentEvent], None]


class AgentEventBus:
    """Fan-out of agent events to zero or more listeners.

    One bus is created per `delegate` batch and shared by every agent in it,
    so the live view sees the whole batch. Listener callbacks are isolated:
    a raising listener is dropped for that event, never propagated back into
    the agent loop (rendering must never crash an agent).
    """

    def __init__(self) -> None:
        self._listeners: List[AgentEventListener] = []

    def subscribe(self, listener: AgentEventListener) -> Callable[[], None]:
        """Register a listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _unsubscribe

    def emit(self, event: AgentEvent) -> None:
        """Deliver an event to every listener, swallowing listener errors."""
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # pragma: no cover - defensive; never crash a run
                # A broken live view must not take down the agent it renders.
                pass


@dataclass
class AgentRowState:
    """In-memory render state for one agent's status row.

    The live view owns a dict of these keyed by `agent_run_id` and mutates
    them from bus events; it is not persisted and holds no transcript.
    """

    agent_run_id: str
    agent_type: str
    task: str
    status: str = "starting"
    current_tool: Optional[str] = None
    iterations: int = 0
    started_ts: float = field(default_factory=time.monotonic)
    finished: bool = False
    last_text: str = ""

    def elapsed(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.monotonic()) - self.started_ts

    def apply(self, event: AgentEvent) -> None:
        """Fold a live event into this row's state."""
        kind = event.kind
        if kind == AgentEventKind.STARTED:
            self.status = "running"
        elif kind == AgentEventKind.TOOL_START:
            self.current_tool = event.detail
            self.status = "running"
        elif kind == AgentEventKind.TOOL_END:
            # Keep the last tool name visible but mark it no longer in-flight.
            self.current_tool = None
        elif kind == AgentEventKind.TURN_END:
            self.iterations += 1
            self.current_tool = None
        elif kind == AgentEventKind.ASSISTANT_TEXT:
            self.last_text = event.detail
        elif kind in (AgentEventKind.INFO, AgentEventKind.WARNING):
            if event.detail:
                self.last_text = event.detail
        elif kind == AgentEventKind.ERROR:
            self.status = "error"
            if event.detail:
                self.last_text = event.detail
        elif kind == AgentEventKind.FINISHED:
            self.finished = True
            # The reported status (completed/partial/stuck) rides in detail.
            self.status = event.detail or "finished"
            self.current_tool = None


def build_rows(states: Dict[str, AgentRowState]) -> List[AgentRowState]:
    """Deterministic ordering for rendering: by start time, then id."""
    return sorted(states.values(), key=lambda s: (s.started_ts, s.agent_run_id))
