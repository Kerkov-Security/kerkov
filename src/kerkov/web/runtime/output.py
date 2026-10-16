"""ConversationEngine output sink for browser sessions."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, ContextManager, Optional

from kerkov.core.conversation_engine import OutputSink
from kerkov.models.basic_models import TokenUsage
from kerkov.tools.base_tool import ToolResult
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.markdown_html import render_markdown

_RICH_TAG_RE = re.compile(r"\[/?[^\]]+\]")


def _plain(label: str) -> str:
    return _RICH_TAG_RE.sub("", label).strip()


def _preview(text: str, limit: int = 1000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


class _StatusContext:
    def __init__(self, bus: WebEventBus, label: str) -> None:
        self._bus = bus
        self._label = _plain(label)
        self._started_at = 0.0

    def __enter__(self) -> "_StatusContext":
        self._started_at = time.monotonic()
        self._bus.emit("status.start", {"label": self._label})
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        elapsed = time.monotonic() - self._started_at
        self._bus.emit(
            "status.end",
            {
                "label": self._label,
                "elapsed_seconds": round(elapsed, 2),
            },
        )


class WebOutputSink(OutputSink):
    """Output sink that translates engine callbacks into browser events."""

    def __init__(self, bus: WebEventBus, *, stream: bool = True) -> None:
        self._bus = bus
        self._turn_started_at = 0.0
        self._stream = stream
        self._stream_id: Optional[str] = None
        self._stream_open = False

    @property
    def supports_streaming(self) -> bool:
        return self._stream

    def begin_assistant_stream(self) -> None:
        self._stream_id = uuid.uuid4().hex
        self._stream_open = True
        self._bus.emit("assistant.stream.start", {"stream_id": self._stream_id})

    def assistant_delta(self, text: str) -> None:
        if text and self._stream_open:
            self._bus.emit(
                "assistant.stream.delta",
                {"stream_id": self._stream_id, "text": text},
            )

    def end_assistant_stream(self) -> None:
        if self._stream_open:
            self._stream_open = False
            self._bus.emit("assistant.stream.end", {"stream_id": self._stream_id})

    def start_turn(self) -> None:
        self._turn_started_at = time.monotonic()
        self._bus.emit("turn.start", {})

    def end_turn(self) -> None:
        self.end_assistant_stream()
        self._stream_id = None
        elapsed = time.monotonic() - self._turn_started_at
        self._bus.emit("turn.end", {"elapsed_seconds": round(elapsed, 2)})

    def status(self, label: str) -> ContextManager[Any]:
        return _StatusContext(self._bus, label)

    def info(self, msg: str) -> None:
        if msg.strip():
            self._bus.emit("log.info", {"message": _plain(msg)})

    def warning(self, msg: str) -> None:
        self._bus.emit("log.warning", {"message": _plain(msg)})

    def error(self, msg: str) -> None:
        self._bus.emit("log.error", {"message": _plain(msg)})

    def assistant_text(self, content: str) -> None:
        self._assistant_message(content, final=False)

    def final(self, content: str) -> None:
        self._assistant_message(content, final=True)

    def _assistant_message(self, content: str, *, final: bool) -> None:
        # The completed provider text is authoritative, even if a provider
        # supplied no deltas or its final text differs from the preview.
        payload: dict[str, Any] = {
            "message": content,
            "html": render_markdown(content),
            "final": final,
        }
        if self._stream_id is not None:
            payload["stream_id"] = self._stream_id
        self._stream_id = None
        self._bus.emit(
            "assistant.message",
            payload,
        )

    def tool_start(self, tool_name: str, params: dict) -> None:
        self._bus.emit(
            "tool.start",
            {
                "name": tool_name,
                "params": params,
            },
        )

    def tool_pending_approval(self, tool_name: str, params: dict) -> None:
        self._bus.emit(
            "tool.pending_approval",
            {
                "name": tool_name,
                "params": params,
            },
        )

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self._bus.emit(
            "tool.result",
            {
                "name": tool_name,
                "success": True,
                "output": _preview(result.output or ""),
                "metadata": result.metadata or {},
            },
        )

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        self._bus.emit(
            "tool.result",
            {
                "name": tool_name,
                "success": False,
                "error": result.error or "Tool failed.",
                "output": _preview(result.output or ""),
                "metadata": result.metadata or {},
            },
        )

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        if not usage:
            return
        self._bus.emit(
            "token.usage",
            {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        )
