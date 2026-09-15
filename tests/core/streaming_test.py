"""Streaming assistant output: sink delta path, engine wiring, and interrupt."""

import asyncio
import contextlib
import io
import unittest
from typing import Any, Callable, ContextManager, List, Optional

from rich.console import Console

from kerkov.cli.cancellable_task import InterruptedByUser
from kerkov.core.conversation_engine import (
    ConsoleSink,
    ConversationEngine,
    LoopSession,
    OutputSink,
    QuietSink,
    WhitelistAutoApproval,
)
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage


def _run(coro):
    return asyncio.run(coro)


class _StreamRecordingSink(OutputSink):
    """A streaming-capable sink that records the delta lifecycle."""

    def __init__(self) -> None:
        self.deltas: List[str] = []
        self.begins = 0
        self.ends = 0
        self.finals: List[str] = []

    @property
    def supports_streaming(self) -> bool:
        return True

    def begin_assistant_stream(self) -> None:
        self.begins += 1

    def assistant_delta(self, text: str) -> None:
        self.deltas.append(text)

    def end_assistant_stream(self) -> None:
        self.ends += 1

    def status(self, label: str) -> ContextManager[Any]:
        return contextlib.nullcontext()

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

    def assistant_text(self, content: str) -> None:
        pass

    def final(self, content: str) -> None:
        self.finals.append(content)

    def tool_start(self, tool_name: str, params: dict) -> None:
        pass

    def tool_succeeded(self, tool_name, result) -> None:
        pass

    def tool_failed(self, tool_name, result) -> None:
        pass

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        pass


class _StreamingProvider:
    """Fake provider that forwards chunks to on_delta, then returns/raises."""

    def __init__(self, chunks: List[str], *, interrupt: bool = False) -> None:
        self.chunks = chunks
        self.interrupt = interrupt
        self.on_delta_seen: Any = "unset"
        self.caches_prompt_prefix = False

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        self.on_delta_seen = on_delta
        for chunk in self.chunks:
            if on_delta is not None:
                on_delta(chunk)
        if self.interrupt:
            raise InterruptedByUser()
        return LLMResponse(content="".join(self.chunks), model="fake")

    async def get_max_output_tokens(self) -> Optional[int]:
        return 2048

    async def get_max_context_tokens(self) -> Optional[int]:
        return 100_000


def _session(provider, sink, **kwargs) -> LoopSession:
    return LoopSession(
        provider=provider,
        history=[Message(role="user", content="hi")],
        tools=[],
        approval=WhitelistAutoApproval(),
        output=sink,
        max_iterations=5,
        transient_prompts=[],
        **kwargs,
    )


class EngineStreamingTest(unittest.TestCase):
    def test_streams_deltas_and_records_final(self):
        provider = _StreamingProvider(["Hel", "lo ", "world"])
        sink = _StreamRecordingSink()
        _run(ConversationEngine().run_loop(_session(provider, sink)))

        self.assertEqual(sink.deltas, ["Hel", "lo ", "world"])
        self.assertEqual(sink.begins, 1)
        self.assertEqual(sink.ends, 1)
        self.assertEqual(sink.finals, ["Hello world"])
        self.assertIsNotNone(provider.on_delta_seen)

    def test_non_streaming_sink_passes_no_callback(self):
        provider = _StreamingProvider(["ignored"])
        sink = QuietSink()
        self.assertFalse(sink.supports_streaming)
        _run(ConversationEngine().run_loop(_session(provider, sink)))
        self.assertIsNone(provider.on_delta_seen)

    def test_partial_recorded_and_turn_ends_on_interrupt(self):
        provider = _StreamingProvider(["par", "tial"], interrupt=True)
        sink = _StreamRecordingSink()
        session = _session(provider, sink)

        result = _run(ConversationEngine().run_loop(session))

        self.assertIsNone(result)
        # end_assistant_stream still fires so the terminal is left clean.
        self.assertEqual(sink.ends, 1)
        partials = [m for m in session.history if getattr(m, "partial", False)]
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].role, "assistant")
        self.assertEqual(partials[0].content, "partial")


class ConsoleSinkStreamingTest(unittest.TestCase):
    def _console(self):
        buffer = io.StringIO()
        console = Console(file=buffer, width=200, no_color=True, force_terminal=False)
        return ConsoleSink(console), buffer

    def test_supports_streaming(self):
        sink, _ = self._console()
        self.assertTrue(sink.supports_streaming)

    def test_streamed_then_matching_final_not_double_printed(self):
        sink, buffer = self._console()
        sink.begin_assistant_stream()
        sink.assistant_delta("hello ")
        sink.assistant_delta("world")
        sink.end_assistant_stream()
        sink.final("hello world")

        rendered = buffer.getvalue()
        self.assertEqual(rendered.count("hello world"), 1)

    def test_final_renders_when_nothing_streamed(self):
        sink, buffer = self._console()
        sink.begin_assistant_stream()
        sink.end_assistant_stream()
        sink.final("only final")
        self.assertIn("only final", buffer.getvalue())

    def test_mismatched_final_still_renders(self):
        sink, buffer = self._console()
        sink.begin_assistant_stream()
        sink.assistant_delta("partial text")
        sink.end_assistant_stream()
        sink.final("a completely different full answer")
        self.assertIn("a completely different full answer", buffer.getvalue())

    def test_emoji_shortcodes_in_deltas_are_not_substituted(self):
        sink, buffer = self._console()
        literal = "Scan http://[2001:db8:a::1]:8080/ next."
        sink.begin_assistant_stream()
        sink.assistant_delta(literal)
        sink.end_assistant_stream()
        self.assertIn(literal, buffer.getvalue())


class SinkDefaultsTest(unittest.TestCase):
    def test_base_defaults_are_non_streaming_noops(self):
        class _Bare(OutputSink):
            def status(self, label):
                return contextlib.nullcontext()

            def info(self, msg):
                pass

            def warning(self, msg):
                pass

            def error(self, msg):
                pass

            def assistant_text(self, content):
                pass

            def final(self, content):
                pass

            def tool_start(self, tool_name, params):
                pass

            def tool_succeeded(self, tool_name, result):
                pass

            def tool_failed(self, tool_name, result):
                pass

            def token_usage(self, usage):
                pass

        sink = _Bare()
        self.assertFalse(sink.supports_streaming)
        # No-ops must not raise.
        sink.begin_assistant_stream()
        sink.assistant_delta("x")
        sink.end_assistant_stream()

    def test_quietsink_is_non_streaming(self):
        self.assertFalse(QuietSink().supports_streaming)


if __name__ == "__main__":
    unittest.main()
