"""Harness data models — Pydantic schemas for harness.md frontmatter and state.json."""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class HarnessMode(str, Enum):
    TASK_LIST = "task_list"
    GOAL_PREDICATE = "goal_predicate"  # Phase 2


class TargetClass(str, Enum):
    LOCAL = "local"
    CTF = "ctf"
    EXTERNAL = "external"


class Scope(BaseModel):
    in_scope: List[str] = Field(default_factory=list)
    out_of_scope: List[str] = Field(default_factory=list)


class Budget(BaseModel):
    max_iterations: int = Field(default=50)
    max_wallclock_seconds: int = Field(default=14400)
    # Phase 2
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None


class RateLimits(BaseModel):
    """Phase 3 — declarative only in Phase 1."""

    fetch_per_minute: Optional[int] = None
    shell_commands_per_minute: Optional[int] = None


class Task(BaseModel):
    id: str
    description: str
    done: str = Field(description="Predicate string evaluated by the driver")
    passes: bool = False


class SuccessCriterion(BaseModel):
    """A single goal_predicate success check."""

    id: str
    description: str
    check: str = Field(description="Predicate string evaluated by the driver")


class Harness(BaseModel):
    name: str
    description: str = ""
    mode: HarnessMode = HarnessMode.TASK_LIST
    target_class: TargetClass

    scope: Scope = Field(default_factory=Scope)
    budget: Budget = Field(default_factory=Budget)
    allowed_tools: List[str] = Field(default_factory=list)
    forbidden_actions: List[str] = Field(default_factory=list)
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    stuck_after: int = 8

    tasks: List[Task] = Field(default_factory=list)

    # goal_predicate mode fields
    goal: Optional[str] = None
    success_criteria: List[SuccessCriterion] = Field(default_factory=list)
    progress_signal: Optional[str] = None

    body: str = ""

    snapshot_per_iteration: bool = True


class TaskResult(BaseModel):
    id: str
    met: bool


class HarnessStatus(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    STUCK = "stuck"
    ERROR = "error"


class HarnessState(BaseModel):
    """Cross-iteration state, written by HarnessStateTool, read by driver."""

    iteration: int = 0
    status: HarnessStatus = HarnessStatus.CONTINUE
    last_progress_iteration: int = 0
    tokens_used_total: int = 0
    tokens_used_this_iteration: int = 0
    wallclock_seconds_total: int = 0
    cost_usd_total: float = 0.0
    cost_usd_this_iteration: float = 0.0
    # None when pricing is unknown for the active model — caller should treat
    # cost budgets as unenforceable rather than zero.
    cost_tracking_enabled: bool = True
    notes: str = ""
    success_criteria_results: List[TaskResult] = Field(default_factory=list)
