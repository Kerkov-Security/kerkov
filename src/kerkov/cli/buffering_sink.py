"""Master narration held back while the terminal is showing an agent.

Attaching points the terminal at one agent, but the master's turn keeps
running underneath it. Without this the two conversations interleave line by
line and neither is readable. `BufferingSink` records what the master would
have printed and replays it, in order, once focus returns.
"""

import contextlib
from typing import Any, ContextManager, List, Optional, Tuple

from kerkov.core.conversation_engine import ConsoleSink, OutputSink
from kerkov.models.basic_models import TokenUsage
from kerkov.tools.base_tool import ToolResult

# A ten-minute attach to a busy agent would otherwise queue the master's whole
# turn. Dropping the oldest keeps the tail — the part that still matters.
MAX_BUFFERED_EVENTS = 2000


class BufferingSink(OutputSink):
    """Wraps the master's `ConsoleSink`, recording instead of printing on demand."""

    def __init__(
        self,
        inner: ConsoleSink,
        max_events: int = MAX_BUFFERED_EVENTS,
    ) -> None:
        self._inner = inner
        self._max_events = max_events
        self._buffer: List[Tuple[str, Tuple[Any, ...]]] = []
        self._dropped = 0
        self._buffering = False

    @property
    def buffering(self) -> bool:
        return self._buffering

    @property
    def supports_streaming(self) -> bool:
        return self._inner.supports_streaming

    def begin_assistant_stream(self) -> None:
        # While an agent owns the terminal the master's token stream is dropped,
        # not buffered: a replayed token stream is stale, and the completed
        # `final`/`assistant_text` is still recorded and replayed on flush.
        if self._buffering:
            return
        self._inner.begin_assistant_stream()

    def assistant_delta(self, text: str) -> None:
        if self._buffering:
            return
        self._inner.assistant_delta(text)

    def end_assistant_stream(self) -> None:
        if self._buffering:
            return
        self._inner.end_assistant_stream()

    @property
    def pending(self) -> int:
        """How many recorded calls are waiting to be replayed."""

        return len(self._buffer)

    def start_buffering(self) -> None:
        self._buffering = True

    def flush(self) -> None:
        """Stop buffering and replay everything recorded, oldest first."""

        self._buffering = False
        buffered, self._buffer = self._buffer, []
        dropped, self._dropped = self._dropped, 0
        if dropped:
            self._inner.info(
                f"… {dropped} earlier main-conversation line(s) dropped; "
                "the full turn is in the log."
            )
        for name, args in buffered:
            getattr(self._inner, name)(*args)

    def _record(self, name: str, *args: Any) -> None:
        self._buffer.append((name, args))
        if len(self._buffer) > self._max_events:
            del self._buffer[0]
            self._dropped += 1

    def status(self, label: str) -> ContextManager[Any]:
        # A spinner holds the terminal live; there is nothing to replay later
        # and a recorded one would come back as a stale line.
        if self._buffering:
            return contextlib.nullcontext()
        return self._inner.status(label)

    def suspend_status(self) -> ContextManager[None]:
        """Delegate: credential prompts reach for this through `getattr`."""

        return self._inner.suspend_status()

    def start_turn(self) -> None:
        # Prints nothing, and the timer it starts has to run in real time or
        # the elapsed replayed by `end_turn` would be measured from the detach.
        self._inner.start_turn()

    def end_turn(self) -> None:
        if self._buffering:
            self._record("end_turn")
            return
        self._inner.end_turn()

    def iteration_start(self, n: int) -> None:
        if self._buffering:
            self._record("iteration_start", n)
            return
        self._inner.iteration_start(n)

    def tool_pending_approval(self, tool_name: str, params: dict) -> None:
        if self._buffering:
            self._record("tool_pending_approval", tool_name, params)
            return
        self._inner.tool_pending_approval(tool_name, params)

    def info(self, msg: str) -> None:
        if self._buffering:
            self._record("info", msg)
            return
        self._inner.info(msg)

    def warning(self, msg: str) -> None:
        if self._buffering:
            self._record("warning", msg)
            return
        self._inner.warning(msg)

    def error(self, msg: str) -> None:
        if self._buffering:
            self._record("error", msg)
            return
        self._inner.error(msg)

    def assistant_text(self, content: str) -> None:
        if self._buffering:
            self._record("assistant_text", content)
            return
        self._inner.assistant_text(content)

    def final(self, content: str) -> None:
        if self._buffering:
            self._record("final", content)
            return
        self._inner.final(content)

    def tool_start(self, tool_name: str, params: dict) -> None:
        if self._buffering:
            self._record("tool_start", tool_name, params)
            return
        self._inner.tool_start(tool_name, params)

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        if self._buffering:
            self._record("tool_succeeded", tool_name, result)
            return
        self._inner.tool_succeeded(tool_name, result)

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        if self._buffering:
            self._record("tool_failed", tool_name, result)
            return
        self._inner.tool_failed(tool_name, result)

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        if self._buffering:
            self._record("token_usage", usage)
            return
        self._inner.token_usage(usage)
