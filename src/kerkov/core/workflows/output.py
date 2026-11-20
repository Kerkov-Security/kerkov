"""Record tool results for a workflow run while the front end still renders."""

from __future__ import annotations

from typing import Any, ContextManager, Optional

from kerkov.core.conversation_engine import OutputSink
from kerkov.core.workflows.context import WorkflowRun
from kerkov.models.basic_models import TokenUsage
from kerkov.tools.base_tool import ToolResult


class WorkflowOutput(OutputSink):
    """Forwarding decorator that also hands each tool result to the run."""

    def __init__(self, run: WorkflowRun, inner: OutputSink) -> None:
        self.run = run
        self.inner = inner

    @property
    def supports_streaming(self) -> bool:
        return self.inner.supports_streaming

    def begin_assistant_stream(self) -> None:
        self.inner.begin_assistant_stream()

    def assistant_delta(self, text: str) -> None:
        self.inner.assistant_delta(text)

    def end_assistant_stream(self) -> None:
        self.inner.end_assistant_stream()

    def status(self, label: str) -> ContextManager[Any]:
        return self.inner.status(label)

    def info(self, msg: str) -> None:
        self.inner.info(msg)

    def warning(self, msg: str) -> None:
        self.inner.warning(msg)

    def error(self, msg: str) -> None:
        self.inner.error(msg)

    def assistant_text(self, content: str) -> None:
        self.inner.assistant_text(content)

    def final(self, content: str) -> None:
        self.inner.final(content)

    def tool_start(self, tool_name: str, params: dict) -> None:
        self.inner.tool_start(tool_name, params)

    def tool_pending_approval(self, tool_name: str, params: dict) -> None:
        self.inner.tool_pending_approval(tool_name, params)

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self.run.result(tool_name, result)
        self.inner.tool_succeeded(tool_name, result)

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        self.run.result(tool_name, result)
        self.inner.tool_failed(tool_name, result)

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        self.inner.token_usage(usage)

    def iteration_start(self, n: int) -> None:
        self.inner.iteration_start(n)

    def start_turn(self) -> None:
        self.inner.start_turn()

    def end_turn(self) -> None:
        self.inner.end_turn()
