"""Tests for BufferingSink: hold the master's narration, replay it in order."""

import contextlib
import io
import unittest
from unittest.mock import MagicMock

from rich.console import Console

from kerkov.cli.buffering_sink import BufferingSink
from kerkov.core.conversation_engine import ConsoleSink, OutputSink
from kerkov.models.basic_models import TokenUsage
from kerkov.tools.base_tool import ToolResult


def _create_mock_inner() -> MagicMock:
    inner = MagicMock(spec=ConsoleSink)
    inner.status.return_value = contextlib.nullcontext()
    inner.suspend_status.return_value = contextlib.nullcontext()
    return inner


def _emit_every_buffered_call(sink: BufferingSink) -> list:
    """Every printing member, in a fixed order, so replay order is checkable."""
    result = ToolResult(success=True, output="ok")
    failure = ToolResult(success=False, output="", error="boom")
    usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    sink.iteration_start(1)
    sink.info("info line")
    sink.warning("warning line")
    sink.error("error line")
    sink.assistant_text("thinking")
    sink.tool_pending_approval("exec", {"cmd": "ls"})
    sink.tool_start("exec", {"cmd": "ls"})
    sink.tool_succeeded("exec", result)
    sink.tool_failed("exec", failure)
    sink.token_usage(usage)
    sink.final("done")
    sink.end_turn()
    return [
        ("iteration_start", (1,)),
        ("info", ("info line",)),
        ("warning", ("warning line",)),
        ("error", ("error line",)),
        ("assistant_text", ("thinking",)),
        ("tool_pending_approval", ("exec", {"cmd": "ls"})),
        ("tool_start", ("exec", {"cmd": "ls"})),
        ("tool_succeeded", ("exec", result)),
        ("tool_failed", ("exec", failure)),
        ("token_usage", (usage,)),
        ("final", ("done",)),
        ("end_turn", ()),
    ]


class BufferingSinkTest(unittest.TestCase):

    def setUp(self):
        self.inner = _create_mock_inner()
        self.sink = BufferingSink(self.inner)

    def test_it_implements_the_whole_output_sink_surface(self):
        # A member missed here is a line that punches through the buffer.
        for name in dir(OutputSink):
            if name.startswith("_"):
                continue
            self.assertIn(name, BufferingSink.__dict__, f"{name} is not overridden")

    def test_calls_pass_straight_through_when_not_buffering(self):
        self.sink.info("hello")

        self.inner.info.assert_called_once_with("hello")
        self.assertEqual(self.sink.pending, 0)

    def test_nothing_reaches_the_console_while_buffering(self):
        self.sink.start_buffering()

        expected = _emit_every_buffered_call(self.sink)

        for name, _ in expected:
            getattr(self.inner, name).assert_not_called()
        self.assertEqual(self.sink.pending, len(expected))

    def test_flush_replays_every_call_in_order(self):
        self.sink.start_buffering()
        expected = _emit_every_buffered_call(self.sink)

        self.sink.flush()

        replayed = [
            (call[0], call.args) for call in self.inner.mock_calls if call[0] != ""
        ]
        # start_turn is not buffered, so nothing else should be interleaved.
        self.assertEqual(replayed, expected)

    def test_flush_clears_the_buffer_and_is_idempotent(self):
        self.sink.start_buffering()
        self.sink.info("once")

        self.sink.flush()
        self.sink.flush()

        self.inner.info.assert_called_once_with("once")
        self.assertEqual(self.sink.pending, 0)
        self.assertFalse(self.sink.buffering)

    def test_status_degrades_to_a_null_context_while_buffering(self):
        self.sink.start_buffering()

        with self.sink.status("thinking"):
            pass

        self.inner.status.assert_not_called()
        self.assertEqual(self.sink.pending, 0)

    def test_status_is_delegated_when_not_buffering(self):
        with self.sink.status("thinking"):
            pass

        self.inner.status.assert_called_once_with("thinking")

    def test_start_turn_is_never_buffered(self):
        self.sink.start_buffering()

        self.sink.start_turn()

        # The timer has to run in real time or the elapsed that `end_turn`
        # replays would be measured from the detach, not from the turn.
        self.inner.start_turn.assert_called_once_with()
        self.assertEqual(self.sink.pending, 0)

    def test_suspend_status_is_delegated(self):
        with self.sink.suspend_status():
            pass

        self.inner.suspend_status.assert_called_once_with()

    def test_overflow_drops_the_oldest_and_says_how_many(self):
        sink = BufferingSink(self.inner, max_events=3)
        sink.start_buffering()
        for n in range(5):
            sink.info(f"line {n}")

        sink.flush()

        printed = [call.args[0] for call in self.inner.info.mock_calls]
        self.assertEqual(
            printed,
            [
                "… 2 earlier main-conversation line(s) dropped; "
                "the full turn is in the log.",
                "line 2",
                "line 3",
                "line 4",
            ],
        )

    def test_the_drop_count_resets_after_a_flush(self):
        sink = BufferingSink(self.inner, max_events=1)
        sink.start_buffering()
        sink.info("a")
        sink.info("b")
        sink.flush()
        self.inner.reset_mock()

        sink.start_buffering()
        sink.info("c")
        sink.flush()

        self.assertEqual([call.args[0] for call in self.inner.info.mock_calls], ["c"])


class BufferingSinkRenderingTest(unittest.TestCase):
    """Through a real `ConsoleSink`: what the terminal actually receives."""

    def setUp(self):
        self.buffer = io.StringIO()
        self.console = Console(file=self.buffer, width=200, no_color=True)
        self.sink = BufferingSink(ConsoleSink(self.console))

    def test_buffered_master_output_lands_only_after_the_flush(self):
        self.sink.start_buffering()
        self.sink.info("master says one")
        self.sink.info("master says two")

        self.assertEqual(self.buffer.getvalue(), "")

        self.sink.flush()

        rendered = self.buffer.getvalue()
        self.assertLess(
            rendered.index("master says one"), rendered.index("master says two")
        )


class BufferingSinkStreamingTest(unittest.TestCase):
    def setUp(self):
        self.inner = _create_mock_inner()
        self.inner.supports_streaming = True
        self.sink = BufferingSink(self.inner)

    def test_supports_streaming_reflects_inner(self):
        self.assertTrue(self.sink.supports_streaming)
        self.inner.supports_streaming = False
        self.assertFalse(self.sink.supports_streaming)

    def test_deltas_delegate_when_not_buffering(self):
        self.sink.begin_assistant_stream()
        self.sink.assistant_delta("hi")
        self.sink.end_assistant_stream()

        self.inner.begin_assistant_stream.assert_called_once()
        self.inner.assistant_delta.assert_called_once_with("hi")
        self.inner.end_assistant_stream.assert_called_once()

    def test_deltas_dropped_while_buffering(self):
        self.sink.start_buffering()
        self.sink.begin_assistant_stream()
        self.sink.assistant_delta("hi")
        self.sink.end_assistant_stream()

        self.inner.begin_assistant_stream.assert_not_called()
        self.inner.assistant_delta.assert_not_called()
        self.inner.end_assistant_stream.assert_not_called()
        # The completed response is still recorded and replays on flush.
        self.sink.final("the whole answer")
        self.sink.flush()
        self.inner.final.assert_called_once_with("the whole answer")


if __name__ == "__main__":
    unittest.main()
