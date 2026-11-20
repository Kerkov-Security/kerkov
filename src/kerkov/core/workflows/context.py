"""Front-end-neutral view of the session a workflow run executes inside."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from kerkov.core.config_manager import ConfigManager
from kerkov.core.conversation_engine import ApprovalStrategy, OutputSink
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.tools.base_tool import ToolResult


class WorkflowObserver(ABC):
    """Where a run's record projection is delivered as it changes."""

    @abstractmethod
    def updated(self, projection: dict[str, Any]) -> None:
        """Report the run record plus its control comparison."""


class WorkflowRun(ABC):
    """A run that records the results of the tool calls it gated."""

    @abstractmethod
    def result(self, tool_name: str, result: ToolResult) -> None:
        """Fold one finished tool result into the run record."""


@dataclass(frozen=True)
class WorkflowContext:
    """What a workflow run needs from its front end, read live on every call."""

    # Callables, not values: a gated call re-checks them, and a snapshot would
    # retire the "engagement or execution settings changed" block that keeps
    # the model inside the approved plan.
    config: ConfigManager
    workspace: Callable[[], Optional[Path]]
    context_id: Callable[[], Any]
    ledger: Callable[[], Optional[FindingsLedger]]
    approval: Callable[[], ApprovalStrategy]
    closed: Callable[[], bool]
    sink: OutputSink
    observer: WorkflowObserver
