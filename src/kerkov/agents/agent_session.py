"""AgentSession — one detached agent run as a live, nameable object.

The supervisor owns the lifecycle fields (`state`, `report`, timings); the
session's own `AgentEventSink` owns the per-iteration status (`iteration`,
`current_tool`) because those only ever surface through the loop's output
hooks. One writer per field, no callbacks threaded through `LoopSession`.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from kerkov.agents.models import AgentRunReport
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.cancellable_task import EventInterrupt, InterruptHandle
from kerkov.core.conversation_engine import QuietSink
from kerkov.core.logging.logger_factory import get_logger
from kerkov.models.basic_models import Message
from kerkov.tools.base_tool import ToolResult

logger = get_logger("AgentSession")

TASK_LABEL_CHARS = 60
NAME_MAX_CHARS = 32
NAME_MAX_WORDS = 4
# Dropped when slugifying an instruction: they carry no identity and eat the
# word budget before the words that do.
_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "of",
        "on",
        "please",
        "report",
        "results",
        "run",
        "the",
        "then",
        "times",
        "to",
        "with",
    }
)


class AgentState(str, Enum):
    """Where a supervised agent is in its lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED_ON_INPUT = "blocked"
    REPORTING = "reporting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset(
    {
        AgentState.QUEUED,
        AgentState.RUNNING,
        AgentState.BLOCKED_ON_INPUT,
        AgentState.REPORTING,
    }
)
FINISHED_STATES = frozenset({AgentState.DONE, AgentState.FAILED, AgentState.CANCELLED})


def truncate_task(text: str, limit: int = TASK_LABEL_CHARS) -> str:
    """One-line, bounded rendering of a task instruction for labels."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slugify_agent_name(text: str) -> str:
    """Kebab-case handle from free text; empty when nothing usable survives.

    The fallback for a `delegate` call that did not name its task. Deliberately
    dumb — the master naming its own task reads far better, this only has to
    beat "a1". Splitting on whitespace before punctuation keeps `10.0.0.5` as
    one word: a bare count is noise, but the host or port a task is about is
    the most identifying thing in it.
    """
    words = []
    for raw in text.lower().split():
        word = re.sub(r"[^0-9a-z]+", "-", raw).strip("-")
        if not word or word.isdigit() or word in _FILLER_WORDS:
            continue
        words.append(word)
        if len(words) >= NAME_MAX_WORDS:
            break
    return "-".join(words)[:NAME_MAX_CHARS].strip("-")


class AgentEventSink(QuietSink):
    """`QuietSink` that keeps its session's status fields current.

    Every buffered line also fans out through `session.notify`, which is the
    hook the attached view subscribes to.
    """

    def __init__(self, session: "AgentSession") -> None:
        super().__init__(agent_logger=get_logger(f"agent:{session.id}"))
        self._session = session

    def iteration_start(self, n: int) -> None:
        self._session.iteration = n
        self._session.notify("iteration")

    def tool_start(self, tool_name: str, params: dict) -> None:
        self._session.current_tool = tool_name
        if tool_name == REPORT_TOOL_NAME:
            self._session.set_state(AgentState.REPORTING)
        super().tool_start(tool_name, params)

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self._session.current_tool = None
        super().tool_succeeded(tool_name, result)

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        self._session.current_tool = None
        super().tool_failed(tool_name, result)

    def _record(self, msg: str) -> None:
        super()._record(msg)
        self._session.notify("output")


@dataclass
class AgentSession:
    """A spawned agent: `a1` internally, `google-pinger` to the user."""

    id: str
    agent_run_id: str
    profile: str
    task: str
    # What the user sees. `id` stays the short internal handle so addressing an
    # agent never depends on a name the model invented.
    name: str = ""
    state: AgentState = AgentState.QUEUED
    # Spawned directly by the master (`parent_depth==0`), not by another agent.
    # Only these reach the master through turn-boundary delivery, so only these
    # justify waking its idle prompt — a nested child reports to its parent.
    top_level: bool = False
    engagement_id: Optional[str] = None
    iteration: int = 0
    max_iterations: Optional[int] = None
    current_tool: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    history: List[Message] = field(default_factory=list)
    inbox: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    # Set by the supervisor to ask the run to stop at its next checkpoint.
    # Constructing the event off-loop is safe: since 3.10 asyncio primitives
    # bind to the running loop at first use, and `spawn()` runs inside it.
    interrupt: InterruptHandle = field(default_factory=EventInterrupt)
    # Distinguishes a run that stopped because we asked from one that simply
    # finished — both return a report through the same path.
    cancel_requested: bool = False
    # Opt-in: put this agent's tool calls to the operator instead of
    # auto-approving them. Only takes effect while they are attached to it —
    # a detached agent has nobody to ask.
    review_tools: bool = False
    # The runner's own tracker, shared so the table and the session total read
    # a live number instead of waiting for the report.
    token_tracker: Optional[TokenTracker] = None
    report: Optional[AgentRunReport] = None
    report_delivered: bool = False
    task_handle: Optional["asyncio.Task"] = None
    on_event: Optional[Callable[["AgentSession", str], None]] = None
    # Spawn-time persistence for failures/cancellation before AgentRunner starts.
    # The supervisor clears this once the runner owns its outcome recording.
    record_unstarted: Optional[Callable[[str], None]] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    routing_tier: Optional[str] = None
    routing_reason: Optional[str] = None
    events: AgentEventSink = field(init=False)

    def __post_init__(self) -> None:
        self.events = AgentEventSink(self)

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_finished(self) -> bool:
        return self.state in FINISHED_STATES

    def elapsed(self) -> float:
        """Seconds since the run started (frozen once it finishes)."""
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def tokens_used(self) -> int:
        """What this agent has billed — live while it runs, final once it ends."""
        if self.token_tracker is not None:
            return self.token_tracker.cumulative_tokens
        return self.report.tokens_used if self.report is not None else 0

    def display_name(self) -> str:
        """What the user is shown — the colloquial name, never the raw id."""
        return self.name or self.id

    def label(self) -> str:
        """`google-pinger (a1, recon)` — for the model, which addresses by id."""
        return f"{self.display_name()} ({self.id}, {self.profile})"

    def set_state(self, state: AgentState) -> None:
        if state == self.state:
            return
        self.state = state
        self.notify("state")

    def notify(self, event: str) -> None:
        """Fan one status change out to the supervisor's subscribers."""
        if self.on_event is None:
            return
        try:
            self.on_event(self, event)
        except Exception as e:
            logger.warning(f"Agent event subscriber failed for {self.id}: {e}")
