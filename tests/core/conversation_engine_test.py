"""Tests for ConversationEngine — the extracted shared tool-execution loop."""

import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import FrozenSet, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console

from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.config_manager import ConfigManager
from kerkov.core.conversation_engine import (
    ConsoleSink,
    ConversationEngine,
    LoopSession,
    OutputSink,
    QuietSink,
    TurnTimer,
    WhitelistAutoApproval,
    _format_seconds,
    _summarize_parameters,
    _write_preview_lexer,
    artifact_kind_for_tool,
    artifact_title,
    build_clipped_metadata,
    extract_raw_blocks,
    format_tool_result,
    history_footprint_bytes,
    looks_like_deferred_execution,
)
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import ArtifactKind, LedgerSection
from kerkov.core.tool_output import split_tool_output
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)
from kerkov.tools.ledger_tool import LedgerTool
from kerkov.tools.read_url_tool import ReadUrlTool


def _run(coro):
    return asyncio.run(coro)


class _RecordingSink(QuietSink):
    """QuietSink with helper counters for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_succeeded_calls: List[str] = []
        self.tool_failed_calls: List[str] = []
        self.final_calls: List[str] = []
        self.warning_calls: List[str] = []

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        super().tool_succeeded(tool_name, result)
        self.tool_succeeded_calls.append(tool_name)

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        super().tool_failed(tool_name, result)
        self.tool_failed_calls.append(tool_name)

    def final(self, content: str) -> None:
        super().final(content)
        self.final_calls.append(content)

    def warning(self, msg: str) -> None:
        super().warning(msg)
        self.warning_calls.append(msg)


class _EchoParams(ToolParams):
    """Params for the echo test tool — accepts an arbitrary text payload."""

    text: str = ""


class _EchoTool(BaseTool):
    """Test tool that echoes its `text` param back as output."""

    def __init__(self) -> None:
        super().__init__("echo", "echo a string back")

    def get_params_class(self):
        return _EchoParams

    async def run(self, toolParams: _EchoParams) -> ToolResult:
        return ToolResult(success=True, output=f"echo:{toolParams.text}")

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def _make_provider(responses: List[LLMResponse]) -> MagicMock:
    """Mock provider whose `complete()` returns the queued responses in order."""
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)
    provider.get_max_output_tokens = AsyncMock(return_value=2048)
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)
    provider.caches_prompt_prefix = False
    return provider


def _make_session(
    provider,
    *,
    tools: Optional[List[BaseTool]] = None,
    history: Optional[List[Message]] = None,
    max_iterations: int = 10,
    transient_prompts: Optional[List[str]] = None,
    compactor: Optional[ContextCompactor] = None,
    token_tracker: Optional[TokenTracker] = None,
    output: Optional[OutputSink] = None,
    interrupt=None,
    interrupt_ends_turn: bool = False,
    stop_exempt_tools: Optional[FrozenSet[str]] = None,
) -> LoopSession:
    return LoopSession(
        provider=provider,
        history=history if history is not None else [],
        tools=tools or [],
        approval=WhitelistAutoApproval(),
        output=output or _RecordingSink(),
        max_iterations=max_iterations,
        transient_prompts=transient_prompts or [],
        compactor=compactor,
        token_tracker=token_tracker,
        interrupt=interrupt,
        interrupt_ends_turn=interrupt_ends_turn,
        stop_exempt_tools=stop_exempt_tools or frozenset(),
    )


class FinalResponseTest(unittest.TestCase):
    def test_appends_final_assistant_message_and_stops(self):
        provider = _make_provider(
            [
                LLMResponse(
                    content="hello world",
                    model="test-model",
                    usage=TokenUsage(
                        prompt_tokens=5, completion_tokens=2, total_tokens=7
                    ),
                )
            ]
        )
        session = _make_session(provider, history=[Message(role="user", content="hi")])

        result = _run(ConversationEngine().run_loop(session))

        self.assertIsNotNone(result)
        self.assertEqual(result.content, "hello world")
        self.assertEqual(session.history[-1].role, "assistant")
        self.assertEqual(session.history[-1].content, "hello world")
        provider.complete.assert_awaited_once()

    def test_final_response_reasoning_is_replayed_on_next_user_turn(self):
        raw_blocks = [
            {
                "role": "assistant",
                "content": "hello world",
                "reasoning_content": "Answer the greeting.",
            }
        ]
        provider = _make_provider(
            [
                LLMResponse(
                    content="hello world",
                    model="test",
                    raw_response={"output": raw_blocks},
                ),
                LLMResponse(content="done", model="test"),
            ]
        )
        session = _make_session(provider, history=[Message(role="user", content="hi")])
        engine = ConversationEngine()

        _run(engine.run_loop(session))
        session.history.append(Message(role="user", content="continue"))
        _run(engine.run_loop(session))

        replayed = provider.complete.await_args_list[1].kwargs["messages"][1]
        self.assertEqual(replayed.raw_blocks, raw_blocks)


class ToolCallLoopTest(unittest.TestCase):
    def test_tool_call_then_final_response(self):
        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "hi"},
                            call_id="c1",
                        )
                    ],
                ),
                LLMResponse(content="done", model="test"),
            ]
        )
        sink = _RecordingSink()
        session = _make_session(provider, tools=[_EchoTool()], output=sink)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(sink.tool_succeeded_calls, ["echo"])
        self.assertEqual(sink.final_calls, ["done"])
        roles = [m.role for m in session.history]
        self.assertEqual(roles, ["assistant", "tool", "assistant"])
        self.assertIn("echo:hi", session.history[1].content)

    def test_missing_tool_appends_error_and_continues_in_next_turn(self):
        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="nope",
                            parameters={},
                            call_id="c1",
                        )
                    ],
                ),
                LLMResponse(content="ok", model="test"),
            ]
        )
        session = _make_session(provider, tools=[])

        _run(ConversationEngine().run_loop(session))

        self.assertIn("Tool 'nope' not found", session.history[1].content)

    def test_terminate_after_tool_stops_remaining_tools_in_same_turn(self):
        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "first"},
                            call_id="c1",
                        ),
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "second"},
                            call_id="c2",
                        ),
                    ],
                )
            ]
        )
        sink = _RecordingSink()
        session = _make_session(provider, tools=[_EchoTool()], output=sink)
        session.terminate_after_tool = lambda call, result: call.call_id == "c1"

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(sink.tool_succeeded_calls, ["echo"])
        tool_msgs = [m for m in session.history if m.role == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("echo:first", tool_msgs[0].content)


class ApprovalTest(unittest.TestCase):
    def test_denied_tool_skips_remaining_tools_in_turn(self):
        class _Deny(WhitelistAutoApproval):
            async def approve(self, tool, params):
                from kerkov.cli.tool_approval_helper import ToolApprovalResult

                return ToolApprovalResult(approved=False, user_message="nope")

        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "a"},
                            call_id="c1",
                        ),
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "b"},
                            call_id="c2",
                        ),
                    ],
                ),
                LLMResponse(content="done", model="test"),
            ]
        )
        session = _make_session(provider, tools=[_EchoTool()])
        session.approval = _Deny()

        _run(ConversationEngine().run_loop(session))

        tool_msgs = [m for m in session.history if m.role == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertIn("User denied", tool_msgs[0].content)
        self.assertIn("Skipped due to prior tool denial", tool_msgs[1].content)


class MaxIterationsTest(unittest.TestCase):
    def test_warns_and_stops_after_cap(self):
        infinite_tool_call = LLMResponse(
            content="",
            model="test",
            tool_calls=[
                ToolCall(tool_name="echo", parameters={"text": "x"}, call_id="c")
            ],
        )
        provider = MagicMock()
        provider.complete = AsyncMock(return_value=infinite_tool_call)
        provider.get_max_output_tokens = AsyncMock(return_value=2048)

        sink = _RecordingSink()
        session = _make_session(
            provider, tools=[_EchoTool()], output=sink, max_iterations=3
        )

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 3)
        self.assertTrue(any("maximum iterations" in w for w in sink.warning_calls))


class TransientRetryTest(unittest.TestCase):
    def test_deferred_plain_text_triggers_retry_in_non_interactive(self):
        raw_blocks = [
            {
                "role": "assistant",
                "content": "I'll do that next.",
                "reasoning_content": "Plan the scan.",
            }
        ]
        provider = _make_provider(
            [
                LLMResponse(
                    content="I'll do that next.",
                    model="test",
                    raw_response={"output": raw_blocks},
                ),
                LLMResponse(content="done", model="test"),
            ]
        )
        session = _make_session(
            provider,
            transient_prompts=["NON-INTERACTIVE MODE"],
            history=[Message(role="user", content="please scan")],
        )

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 2)
        retry_msgs = [
            m
            for m in session.history
            if m.role == "user" and "Execute the necessary tool calls" in m.content
        ]
        self.assertEqual(len(retry_msgs), 1)
        replayed = provider.complete.await_args_list[1].kwargs["messages"][1]
        self.assertEqual(replayed.raw_blocks, raw_blocks)

    def test_no_retry_when_no_transient_prompts(self):
        provider = _make_provider(
            [LLMResponse(content="I'll do that next.", model="test")]
        )
        session = _make_session(provider)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 1)


class CompactionTest(unittest.TestCase):
    def test_triggers_compactor_when_over_threshold(self):
        tracker = TokenTracker()
        tracker.total_tokens = 800
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = [Message(role="system", content="sys")] + [
            Message(role="user" if i % 2 else "assistant", content=f"m{i}")
            for i in range(1, 14)
        ]
        history.append(Message(role="user", content="latest"))

        # First completion: the final assistant reply that lands the loop in the
        # post-turn compaction check. Second completion: the compactor's
        # summarize call (temperature=0, returns a summary string).
        provider = _make_provider(
            [
                LLMResponse(
                    content="final reply",
                    model="test",
                    usage=TokenUsage(
                        prompt_tokens=800, completion_tokens=10, total_tokens=810
                    ),
                ),
                LLMResponse(content="SUMMARY-TEXT", model="test"),
            ]
        )
        session = _make_session(
            provider,
            history=history,
            compactor=compactor,
            token_tracker=tracker,
        )
        original_len = len(session.history)

        _run(ConversationEngine().run_loop(session))

        self.assertLess(len(session.history), original_len)
        self.assertTrue(
            any(
                "CONTEXT SUMMARY" in m.content
                for m in session.history
                if m.role == "assistant"
            )
        )

    def test_skipped_when_no_compactor(self):
        provider = _make_provider([LLMResponse(content="ok", model="test")])
        session = _make_session(provider)

        # Should not raise even though compactor/token_tracker are None.
        _run(ConversationEngine().run_loop(session))

    def test_warns_once_when_over_threshold_but_nothing_compactable(self):
        tracker = TokenTracker()
        tracker.total_tokens = 800
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        # The shape `_check_compaction` actually sees on the tool-call path: a
        # trailing tool run with no boundary after it, so nothing can be
        # compacted without orphaning a tool_result from its tool_use.
        history = [Message(role="system", content="sys")] + [
            Message(role="tool", content=f"t{i}", call_id=f"c{i}") for i in range(8)
        ]
        sink = _RecordingSink()
        session = _make_session(
            _make_provider([]),
            history=history,
            compactor=compactor,
            token_tracker=tracker,
            output=sink,
        )
        engine = ConversationEngine()

        _run(engine._check_compaction(session))
        _run(engine._check_compaction(session))

        self.assertEqual(len(session.history), 9)
        self.assertEqual(
            [w for w in sink.warning_calls if "compacted" in w],
            [
                "Context is over the compaction threshold but nothing could be "
                "compacted — recent messages are filling the window"
            ],
        )

    def test_summarization_call_is_billed_to_cumulative_spend_only(self):
        """Issue #6: a compaction call is paid for; its prompt is not the size."""
        tracker = TokenTracker()
        tracker.total_tokens = 800
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = [Message(role="system", content="sys")] + [
            Message(role="user" if i % 2 else "assistant", content=f"m{i}")
            for i in range(1, 14)
        ]
        history.append(Message(role="user", content="latest"))

        provider = _make_provider(
            [
                LLMResponse(
                    content="final reply",
                    model="test",
                    usage=TokenUsage(
                        prompt_tokens=800, completion_tokens=10, total_tokens=810
                    ),
                ),
                LLMResponse(
                    content="SUMMARY-TEXT",
                    model="test",
                    usage=TokenUsage(
                        prompt_tokens=500, completion_tokens=20, total_tokens=520
                    ),
                ),
            ]
        )
        session = _make_session(
            provider,
            history=history,
            compactor=compactor,
            token_tracker=tracker,
        )

        _run(ConversationEngine().run_loop(session))

        # The completion (810) plus the summarizer's own call (520).
        self.assertEqual(tracker.cumulative_tokens, 1330)
        # Size is untouched by the summarizer's prompt.
        self.assertEqual(tracker.total_tokens, 810)


class RecordUsageTest(unittest.TestCase):
    def test_token_tracker_updated_from_response_usage(self):
        tracker = TokenTracker()
        provider = _make_provider(
            [
                LLMResponse(
                    content="ok",
                    model="test",
                    usage=TokenUsage(
                        prompt_tokens=42, completion_tokens=8, total_tokens=50
                    ),
                )
            ]
        )
        session = _make_session(provider, token_tracker=tracker)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(tracker.total_tokens, 50)


class MaxResponseTokensTest(unittest.TestCase):
    def test_uses_configured_value_when_set(self):
        provider = _make_provider([LLMResponse(content="ok", model="test")])
        session = _make_session(provider)
        session.max_response_tokens = 256

        _run(ConversationEngine().run_loop(session))

        _, kwargs = provider.complete.call_args
        self.assertEqual(kwargs["max_tokens"], 256)

    def test_falls_back_to_provider_default(self):
        provider = _make_provider([LLMResponse(content="ok", model="test")])
        session = _make_session(provider)

        _run(ConversationEngine().run_loop(session))

        _, kwargs = provider.complete.call_args
        self.assertEqual(kwargs["max_tokens"], 2048)


class MessagesForProviderTest(unittest.TestCase):
    def test_transient_prompts_appended_to_history(self):
        provider = _make_provider([LLMResponse(content="ok", model="test")])
        session = _make_session(
            provider,
            history=[Message(role="user", content="hi")],
            transient_prompts=["EXTRA SYSTEM PROMPT"],
        )

        _run(ConversationEngine().run_loop(session))

        _, kwargs = provider.complete.call_args
        sent = kwargs["messages"]
        self.assertEqual(sent[-1].role, "system")
        self.assertEqual(sent[-1].content, "EXTRA SYSTEM PROMPT")


class TurnTimerTest(unittest.TestCase):
    def test_elapsed_is_zero_before_start(self):
        timer = TurnTimer()
        self.assertEqual(timer.elapsed(), 0.0)

    def test_elapsed_increases_after_start(self):
        import time

        timer = TurnTimer()
        timer.start()
        time.sleep(0.02)
        self.assertGreater(timer.elapsed(), 0.0)

    def test_formatted_ends_with_s(self):
        timer = TurnTimer()
        timer.start()
        self.assertTrue(timer.formatted().endswith("s"))


class ConsoleSinkStatusTest(unittest.TestCase):
    def test_interactive_prompt_can_suspend_and_restore_active_status(self):
        console = MagicMock()
        console.is_terminal = True
        status = MagicMock()

        @contextlib.contextmanager
        def fake_status(*args, **kwargs):
            yield status

        sink = ConsoleSink(console)
        with patch(
            "kerkov.core.conversation_engine._make_timed_status",
            side_effect=fake_status,
        ):
            with sink.status("Running web_search"):
                with sink.suspend_status():
                    status.stop.assert_called_once_with()
                    status.start.assert_not_called()
                status.start.assert_called_once_with()

        self.assertIsNone(sink._active_status)


class ConsoleSinkMarkupTest(unittest.TestCase):
    """Tool names, params and output are data, never Rich markup."""

    def _create_console(self) -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=True,
            width=200,
            height=24,
            color_system=None,
            legacy_windows=False,
            _environ={"TERM": "xterm-256color"},
        )

    def test_tool_start_escapes_params(self):
        console = self._create_console()
        ConsoleSink(console).tool_start("exec", {"command": "echo [/dim]"})
        self.assertIn("echo [/dim]", console.file.getvalue())

    def test_write_start_renders_multiline_syntax_preview(self):
        console = self._create_console()
        ConsoleSink(console).tool_start(
            "write",
            {
                "file_path": "scripts/check.py",
                "content": "print('first')\nprint('second')\n",
            },
        )

        output = console.file.getvalue()
        self.assertIn("write → scripts/check.py (py, 2 lines)", output)
        self.assertIn("print('first')", output)
        self.assertIn("print('second')", output)
        self.assertNotIn("\\nprint", output)

    def test_write_start_truncates_long_previews(self):
        console = self._create_console()
        content = "\n".join(f"line {number}" for number in range(32))
        ConsoleSink(console).tool_start(
            "write", {"file_path": "script.sh", "content": content}
        )

        output = console.file.getvalue()
        self.assertIn("line 29", output)
        self.assertNotIn("line 30", output)
        self.assertIn("… +2 more lines", output)

    def test_write_start_bounds_rendered_rows_for_long_source_lines(self):
        for width in (40, 100, 200):
            for content in (
                "x" * 40000,
                "\n".join(["x" * 2000] * 30),
                "\n".join(["x" * 2000] * 300),
            ):
                with self.subTest(width=width, source_lines=content.count("\n") + 1):
                    console = Console(
                        file=io.StringIO(), width=width, color_system=None
                    )
                    ConsoleSink(console).tool_start(
                        "write", {"file_path": "x.txt", "content": content}
                    )

                    output = console.file.getvalue()
                    self.assertLessEqual(len(output.splitlines()), 35)
                    self.assertTrue(
                        all(len(row) <= width for row in output.splitlines())
                    )
                    if content.count("\n") == 299:
                        self.assertIn("… +270 more lines", output)

    def test_write_start_skips_empty_preview_panel(self):
        console = self._create_console()
        ConsoleSink(console).tool_start(
            "write", {"file_path": "empty.txt", "content": ""}
        )

        self.assertEqual(
            console.file.getvalue(), "→ Executing: write → empty.txt (txt, 0 lines)\n"
        )

    def test_write_start_keeps_crlf_bytes_visible(self):
        console = self._create_console()
        ConsoleSink(console).tool_start(
            "write", {"file_path": "request.txt", "content": "GET / HTTP/1.1\r\n\r\n"}
        )

        output = console.file.getvalue()
        self.assertIn("write → request.txt (txt, 2 lines)", output)
        self.assertIn("GET / HTTP/1.1\\r", output)
        self.assertEqual(output.count("\\r"), 2)
        self.assertNotIn("\r", output)

    def test_write_start_escapes_bidi_controls_in_preview(self):
        console = self._create_console()
        ConsoleSink(console).tool_start(
            "write",
            {
                "file_path": "scripts/check.py",
                "content": 'print("safe")  # \u202e deceptive',
            },
        )

        output = console.file.getvalue()
        self.assertIn('print("safe")', output)
        self.assertIn("\\u202e deceptive", output)
        self.assertNotIn("\u202e", output)

    def test_write_start_preserves_non_lf_separators_as_escapes(self):
        console = self._create_console()
        controls = "first\u2028second\u0085third\rfourth\vfifth\fsixth"
        ConsoleSink(console).tool_start(
            "write",
            {"file_path": "scripts/check.py", "content": controls},
        )

        output = console.file.getvalue()
        self.assertIn("write → scripts/check.py (py, 1 line)", output)
        self.assertIn(
            "first\\u2028second\\x85third\\rfourth\\x0bfifth\\x0csixth",
            output,
        )
        for control in ("\u2028", "\u0085", "\r", "\v", "\f"):
            self.assertNotIn(control, output)

    def test_tool_succeeded_escapes_output(self):
        console = self._create_console()
        result = ToolResult(success=True, output="matched [red] in body")
        ConsoleSink(console).tool_succeeded("exec", result)
        self.assertIn("matched [red] in body", console.file.getvalue())

    def test_tool_failed_escapes_error(self):
        console = self._create_console()
        result = ToolResult(success=False, output="", error="bad tag [/bold]")
        ConsoleSink(console).tool_failed("exec", result)
        self.assertIn("bad tag [/bold]", console.file.getvalue())


class WritePreviewLexerTest(unittest.TestCase):
    def test_filename_detection_covers_scripts_and_configuration(self):
        for path, content, expected in (
            ("scripts/check.py", "print(1)", "python"),
            ("compose.yaml", "services:", "yaml"),
            ("Dockerfile", "FROM alpine", "docker"),
            ("Makefile", "all:", "make"),
            ("settings.json", '{"enabled": true}', "json"),
            ("scan.nse", 'description = "scan"', "lua"),
            ("runner", "#!/usr/bin/env python3\nprint(1)", "python"),
            ("runner", "#!/bin/bash\necho ok", "bash"),
            ("unknown.extension", "plain text", "text"),
        ):
            with self.subTest(path=path, content=content):
                self.assertEqual(_write_preview_lexer(path, content), expected)


class QuietSinkWriteSummaryTest(unittest.TestCase):
    def test_write_body_is_omitted_from_agent_buffer_and_log(self):
        agent_logger = MagicMock()
        sink = QuietSink(agent_logger=agent_logger)
        sink.tool_start(
            "write",
            {
                "file_path": "script.py",
                "content": "print('script body')",
                "append": True,
            },
        )

        self.assertEqual(sink.buffer, ["→ append → script.py (py, 1 line)"])
        agent_logger.info.assert_called_once_with(sink.buffer[0])


class TurnLifecycleTest(unittest.TestCase):
    def test_run_loop_calls_start_and_end_turn(self):
        provider = _make_provider([LLMResponse(content="ok", model="test")])
        start_calls: List[str] = []
        end_calls: List[str] = []

        class _TrackingSink(_RecordingSink):
            def start_turn(self) -> None:
                start_calls.append("start")

            def end_turn(self) -> None:
                end_calls.append("end")

        session = _make_session(provider, output=_TrackingSink())
        _run(ConversationEngine().run_loop(session))

        self.assertEqual(start_calls, ["start"])
        self.assertEqual(end_calls, ["end"])

    def test_end_turn_called_even_on_interrupt(self):
        from kerkov.core.cancellable_task import InterruptedByUser

        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=InterruptedByUser)
        provider.get_max_output_tokens = AsyncMock(return_value=2048)

        end_calls: List[str] = []

        class _TrackingSink(_RecordingSink):
            def end_turn(self) -> None:
                end_calls.append("end")

        session = _make_session(provider, output=_TrackingSink())
        _run(ConversationEngine().run_loop(session))

        self.assertEqual(end_calls, ["end"])


class InterruptEndsTurnTest(unittest.TestCase):
    """`interrupt_ends_turn` is off for the master — that must stay true.

    Escape on the master means "stop this tool, keep the turn". Agents flip the
    flag to get "stop the run"; nothing about the master path may shift.
    """

    def _interrupting_tool_session(self, **kwargs):
        from kerkov.core.cancellable_task import EventInterrupt, InterruptedByUser

        interrupt = EventInterrupt()

        class _InterruptingTool(_EchoTool):
            async def run(self, toolParams):
                # What `run_cancellable` does for real: the event is already set
                # by whoever asked for the stop when the exception surfaces.
                interrupt.interrupt_requested.set()
                raise InterruptedByUser()

        provider = _make_provider(
            [
                _tool_response("echo", {"text": "hi"}),
                LLMResponse(content="carried on", model="test", tool_calls=None),
            ]
        )
        session = _make_session(
            provider,
            tools=[_InterruptingTool()],
            interrupt=interrupt,
            **kwargs,
        )
        return provider, interrupt, session

    def test_master_absorbs_a_tool_interrupt_and_finishes_the_turn(self):
        provider, interrupt, session = self._interrupting_tool_session()

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 2)
        self.assertFalse(interrupt.interrupt_requested.is_set())
        self.assertIn("carried on", [m.content for m in session.history])

    def test_an_agent_stops_after_a_tool_interrupt(self):
        provider, interrupt, session = self._interrupting_tool_session(
            interrupt_ends_turn=True
        )

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 1)
        # Left set so the caller can tell a cancelled run from a finished one.
        self.assertTrue(interrupt.interrupt_requested.is_set())

    def test_an_agent_stops_before_a_completion_when_the_interrupt_is_preset(self):
        """A cancel landing between awaits raises nowhere; the loop top sees it."""
        from kerkov.core.cancellable_task import EventInterrupt

        provider = _make_provider([LLMResponse(content="never", model="test")])
        interrupt = EventInterrupt()
        interrupt.interrupt_requested.set()
        session = _make_session(provider, interrupt=interrupt, interrupt_ends_turn=True)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(provider.complete.await_count, 0)
        self.assertEqual(session.history, [])


class StopBeforeEachToolTest(unittest.TestCase):
    """A stop that lands mid-batch must not start the next tool.

    The interrupt only raises out of a tool that yields; a tool that finishes
    synchronously wins its race with the watcher and lands its side effect, so
    the flag has to be checked before the call is started at all.
    """

    def _batch_session(self, **kwargs):
        from kerkov.core.cancellable_task import EventInterrupt

        interrupt = EventInterrupt()
        ran: List[str] = []

        class _RecordingTool(_EchoTool):
            def __init__(self, name: str, stops: bool = False) -> None:
                BaseTool.__init__(self, name, "record that it ran")
                self._stops = stops

            async def run(self, toolParams):
                if self._stops:
                    interrupt.interrupt_requested.set()
                ran.append(self.name)
                return ToolResult(success=True, output=f"{self.name}:ok")

        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(tool_name="first", parameters={}, call_id="c1"),
                        ToolCall(tool_name="second", parameters={}, call_id="c2"),
                    ],
                ),
                LLMResponse(content="carried on", model="test"),
            ]
        )
        session = _make_session(
            provider,
            tools=[_RecordingTool("first", stops=True), _RecordingTool("second")],
            interrupt=interrupt,
            **kwargs,
        )
        return provider, ran, session

    def test_an_agent_stop_skips_the_rest_of_the_batch(self):
        provider, ran, session = self._batch_session(interrupt_ends_turn=True)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(ran, ["first"])
        self.assertEqual(provider.complete.await_count, 1)
        skipped = [m for m in session.history if m.call_id == "c2"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("stopped at the operator's request", skipped[0].content)

    def test_a_stop_exempt_tool_still_runs_so_its_findings_survive(self):
        """`report` is how a stopped agent hands back what it already found."""
        provider, ran, session = self._batch_session(
            interrupt_ends_turn=True, stop_exempt_tools=frozenset({"second"})
        )

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(ran, ["first", "second"])
        self.assertEqual(provider.complete.await_count, 1)

    def test_the_master_runs_the_whole_batch_and_keeps_its_turn(self):
        """Escape on the master is "stop this tool", never "abandon the batch"."""
        provider, ran, session = self._batch_session()

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(ran, ["first", "second"])
        self.assertEqual(provider.complete.await_count, 2)
        self.assertIn("carried on", [m.content for m in session.history])


class ToolPendingApprovalTest(unittest.TestCase):
    def test_pending_approval_called_before_approve(self):
        provider = _make_provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="echo",
                            parameters={"text": "hi"},
                            call_id="c1",
                        )
                    ],
                ),
                LLMResponse(content="done", model="test"),
            ]
        )
        order: List[str] = []

        class _TrackingApproval(WhitelistAutoApproval):
            async def approve(self, tool, params):
                order.append("approve")
                return await super().approve(tool, params)

        class _TrackingSink(_RecordingSink):
            def tool_pending_approval(self, tool_name: str, params: dict) -> None:
                order.append("pending")

        session = _make_session(provider, tools=[_EchoTool()], output=_TrackingSink())
        session.approval = _TrackingApproval()

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(order, ["pending", "approve"])


class FreeFunctionsTest(unittest.TestCase):
    def test_extract_raw_blocks_handles_anthropic_style_obj(self):
        obj = MagicMock(content=[{"type": "text"}])
        self.assertEqual(extract_raw_blocks(obj), [{"type": "text"}])

    def test_extract_raw_blocks_handles_dict_with_output_list(self):
        self.assertEqual(
            extract_raw_blocks({"output": [{"t": "x"}]}),
            [{"t": "x"}],
        )

    def test_extract_raw_blocks_returns_none_for_none(self):
        self.assertIsNone(extract_raw_blocks(None))

    def test_format_tool_result_includes_status_output_and_error(self):
        result = ToolResult(
            success=False, output="some out", error="boom", metadata={"k": "v"}
        )
        formatted = format_tool_result("toolX", result)
        self.assertIn("Tool 'toolX'", formatted)
        self.assertIn("Status: Failed", formatted)
        self.assertIn("some out", formatted)
        self.assertIn("Error: boom", formatted)
        self.assertIn("Metadata:", formatted)

    def test_format_tool_result_puts_output_last_for_eviction(self):
        # Compaction replaces only the output body, so everything else must sit
        # ahead of the marker `split_tool_output` cuts on.
        result = ToolResult(
            success=False, output="some out", error="boom", metadata={"k": "v"}
        )

        header, body = split_tool_output(format_tool_result("toolX", result))

        self.assertEqual(body, "some out")
        self.assertIn("Status: Failed", header)
        self.assertIn("Error: boom", header)
        self.assertIn("Metadata:", header)

    def test_looks_like_deferred_execution_markers(self):
        self.assertTrue(looks_like_deferred_execution("I'll scan now."))
        self.assertTrue(looks_like_deferred_execution("Let me check this."))
        self.assertFalse(looks_like_deferred_execution("Done."))


class _BigEchoTool(BaseTool):
    """Test tool that returns `size` bytes of output."""

    def __init__(self) -> None:
        super().__init__("bigecho", "emit a lot of text")

    def get_params_class(self):
        return _BigEchoParams

    async def run(self, toolParams: "_BigEchoParams") -> ToolResult:
        body = "\n".join(f"line {i} tomcat" for i in range(toolParams.lines))
        return ToolResult(success=True, output=body)

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams) -> Optional[str]:
        return None


class _BigEchoParams(ToolParams):
    lines: int = 1


class _ParsedJsonTool(BaseTool):
    """Test tool shaped like HTTPTool: a body, plus a parsed copy in metadata."""

    def __init__(self) -> None:
        super().__init__("http", "fetch a JSON document")

    def get_params_class(self):
        return _BigEchoParams

    async def run(self, toolParams: "_BigEchoParams") -> ToolResult:
        records = [f"tomcat-{i}" for i in range(toolParams.lines)]
        return ToolResult(
            success=True,
            output=json.dumps(records),
            metadata={"status": 200, "parsed": records},
        )

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def _tool_call(tool_name: str, parameters: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(tool_name=tool_name, parameters=parameters, call_id=call_id)


def _tool_response(tool_name: str, parameters: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        model="test",
        tool_calls=[_tool_call(tool_name, parameters)],
    )


class KnowledgeBaseIngestionTest(unittest.TestCase):
    """The write-path hook: index everything, clip only what is large."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _session(self, provider, tools, *, threshold: Optional[int] = 8000, **kwargs):
        session = _make_session(provider, tools=tools, **kwargs)
        session.knowledge_base = self.kb
        session.kb_clip_threshold_bytes = threshold
        return session

    def test_small_output_passes_through_inline_and_is_still_indexed(self):
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [_EchoTool()])

        _run(ConversationEngine().run_loop(session))

        tool_message = session.history[1]
        self.assertIn("echo:hi", tool_message.content)
        self.assertNotIn("artifact#", tool_message.content)
        # Indexed anyway: cheap to store, and worth recalling an hour later.
        self.assertEqual(_run(self.kb.count()), 1)
        self.assertEqual(len(_run(self.kb.search("echo"))), 1)

    def test_sub_threshold_result_still_gets_its_artifact_stamped(self):
        # The stamp is what lets compaction later evict a result that was too
        # small to clip at ingest but is worth dropping in bulk.
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [_EchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(session.history[1].artifact_ref, 1)

    def test_no_artifact_is_stamped_without_a_knowledge_base(self):
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        session = _make_session(provider, tools=[_EchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertIsNone(session.history[1].artifact_ref)

    def test_confirmed_ssh_evidence_can_record_runner_and_host_to_host_routes(self):
        for source_is_host in (False, True):
            with self.subTest(source_is_host=source_is_host):
                ledger = FindingsLedger(self.kb)
                target = _run(
                    ledger.add(
                        LedgerSection.TARGETS,
                        {"kind": "ip", "value": "192.0.2.22"},
                        engagement_id="root-1",
                    )
                )
                source = {"source_vantage": "Kerkov runner"}
                if source_is_host:
                    source_id = _run(
                        ledger.add(
                            LedgerSection.TARGETS,
                            {"kind": "ip", "value": "192.0.2.10"},
                            engagement_id="root-1",
                        )
                    )
                    source = {"source_id": source_id}
                observation = _EchoTool()
                observation.name = "process"
                ledger_tool = LedgerTool(lambda: ledger, lambda: "root-1")
                evidence_ids = []

                async def respond(**kwargs):
                    messages = kwargs["messages"]
                    self.assertIn("NETWORK MAP", messages[0].content)
                    results = [
                        message for message in messages if message.role == "tool"
                    ]
                    if not results:
                        return _tool_response(
                            "process", {"text": "Authenticated to 192.0.2.22; uid=1000"}
                        )
                    if len(results) == 1:
                        evidence = results[0].artifact_ref
                        evidence_ids.append(evidence)
                        header, body = split_tool_output(results[0].content)
                        self.assertIn(f"artifact_ref={evidence}", header)
                        self.assertIn("uid=1000", body)
                        return _tool_response(
                            "ledger",
                            {
                                "op": "add",
                                "section": "connections",
                                "entry": {
                                    **source,
                                    "target_id": target,
                                    "relation": "reaches",
                                    "provenance": "observed",
                                    "protocol": "tcp",
                                    "port": 22,
                                    "service": "SSH",
                                    "note": "Successful SSH authentication and remote id",
                                },
                                "artifact_ref": evidence,
                            },
                        )
                    return LLMResponse(content="Connected.", model="test")

                provider = _make_provider([])
                provider.complete = AsyncMock(side_effect=respond)
                session = self._session(provider, [observation, ledger_tool])
                session.ledger = ledger
                _run(ConversationEngine().run_loop(session))
                graph = _run(ledger.graph())
                routes = [
                    edge for edge in graph["edges"] if edge["relation"] == "reaches"
                ]
                route = next(
                    edge
                    for edge in routes
                    if edge["evidence"][0]["artifact_ref"] == evidence_ids[0]
                )
                nodes = {node["id"]: node for node in graph["nodes"]}
                self.assertEqual(
                    nodes[route["source"]]["value"],
                    "192.0.2.10" if source_is_host else "Kerkov runner",
                )
                self.assertEqual(nodes[route["target"]]["value"], "192.0.2.22")
                self.assertEqual(route["service"], "SSH")
                self.assertEqual(route["provenances"], ["observed"])
                self.assertEqual(route["port"], 22)

    def test_process_tool_success_does_not_infer_ssh_access_from_prompts_or_failure(
        self,
    ):
        ledger = FindingsLedger(self.kb)
        ledger_tool = LedgerTool(lambda: ledger, lambda: "root-1")
        observation = _EchoTool()
        observation.name = "process"
        for text in (
            "Process started",
            "Host key fingerprint",
            "Password:",
            "Permission denied",
        ):
            with self.subTest(text=text):
                provider = _make_provider(
                    [
                        _tool_response("process", {"text": text}),
                        LLMResponse(content="", model="t"),
                    ]
                )
                session = self._session(provider, [observation, ledger_tool])
                session.ledger = ledger
                _run(ConversationEngine().run_loop(session))
                header, body = split_tool_output(session.history[1].content)
                self.assertIn("Status: Success", header)
                self.assertIn("artifact_ref=", header)
                self.assertIn(text, body)
                self.assertEqual(_run(ledger.list(LedgerSection.CONNECTIONS)), [])

    def test_failed_evidence_storage_never_advertises_an_artifact_handle(self):
        ledger = FindingsLedger(self.kb)
        observation = _EchoTool()
        observation.name = "process"
        provider = _make_provider(
            [
                _tool_response("process", {"text": "Password:"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(
            provider, [observation, LedgerTool(lambda: ledger, lambda: "root-1")]
        )
        session.ledger = ledger
        with patch.object(
            self.kb, "add_artifact", new=AsyncMock(side_effect=OSError("offline"))
        ):
            _run(ConversationEngine().run_loop(session))
        self.assertIsNone(session.history[1].artifact_ref)
        self.assertNotIn("Evidence: artifact#", session.history[1].content)

    def test_network_evidence_handles_require_an_available_ledger_and_tool(self):
        ledger = FindingsLedger(self.kb)
        ledger_tool = LedgerTool(lambda: ledger, lambda: "root-1")
        for name in ("exec", "process", "http", "browser", "container_lifecycle"):
            for enabled in (False, True):
                with self.subTest(tool=name, enabled=enabled):
                    observation = _EchoTool()
                    observation.name = name
                    provider = _make_provider(
                        [
                            _tool_response(name, {"text": "short observation"}),
                            LLMResponse(content="", model="t"),
                        ]
                    )
                    session = self._session(provider, [observation, ledger_tool])
                    session.ledger = ledger if enabled else None
                    _run(ConversationEngine().run_loop(session))
                    content = session.history[1].content
                    self.assertEqual("Evidence: artifact#" in content, enabled)
                    self.assertEqual(
                        _run(
                            self.kb.get_artifact(session.history[1].artifact_ref)
                        ).body,
                        "echo:short observation",
                    )

    def test_large_output_is_replaced_by_a_reference_with_a_preview(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_BigEchoTool()])

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn("artifact#1", content)
        self.assertIn("recall(query='artifact#1')", content)
        self.assertIn("characters elided", content)
        # Head and tail previews both survive, so the model can judge the blob.
        self.assertIn("line 0 tomcat", content)
        self.assertIn("line 1999 tomcat", content)
        # The default KB path already retained ~2000 output characters before
        # normalization. Keep that budget plus room for annotations and status.
        self.assertLess(len(content), 2500)

    def test_ledger_graph_pages_stay_complete_in_history(self):
        ledger = FindingsLedger(self.kb)
        for index in range(75):
            _run(
                ledger.add(
                    LedgerSection.ASSETS,
                    {"kind": "domain", "value": f"api{index}.example.com"},
                    engagement_id="root-1",
                )
            )
        tool = LedgerTool(lambda: ledger, lambda: "root-1")
        first_params = {"op": "graph"}
        first = _run(tool.execute(first_params))
        next_offset = json.loads(first.output)["next_offset"]
        self.assertIsNotNone(next_offset)
        second_params = {"op": "graph", "offset": next_offset}
        second = _run(tool.execute(second_params))
        self.assertIsNone(json.loads(second.output)["next_offset"])
        for kb_state in ("working", "disabled", "failed"):
            with self.subTest(kb_state=kb_state):
                provider = _make_provider(
                    [
                        _tool_response("ledger", first_params),
                        _tool_response("ledger", second_params),
                        LLMResponse(content="", model="t"),
                    ]
                )
                session = self._session(provider, [tool], threshold=32)
                session.tool_output_max_chars = 64
                if kb_state == "disabled":
                    session.knowledge_base = None
                elif kb_state == "failed":
                    session.knowledge_base = MagicMock()
                    session.knowledge_base.root_engagement_id = "root-1"
                    session.knowledge_base.add_artifact = AsyncMock(
                        side_effect=RuntimeError("disk full")
                    )
                _run(ConversationEngine().run_loop(session))
                messages = [m for m in session.history if m.role == "tool"]
                self.assertEqual(len(messages), 2)
                for message, page in zip(messages, (first, second)):
                    self.assertEqual(
                        message.content, format_tool_result("ledger", page)
                    )
                    self.assertEqual(
                        json.loads(split_tool_output(message.content)[1]),
                        json.loads(page.output),
                    )
                    if kb_state == "working":
                        stored = _run(self.kb.get_artifact(message.artifact_ref))
                        self.assertEqual(stored.body, page.output)
                    else:
                        self.assertIsNone(message.artifact_ref)

    def test_ledger_profile_pages_stay_complete_in_history(self):
        ledger = FindingsLedger(self.kb)
        entry_id = _run(
            ledger.add(
                LedgerSection.ASSETS,
                {"kind": "domain", "value": "example.com"},
                engagement_id="root-1",
            )
        )
        for index in range(8):
            _run(
                ledger.add(
                    LedgerSection.FINDINGS,
                    {
                        "host": "example.com",
                        "title": f"Finding {index}",
                        "description": "Detailed finding. " * 70,
                    },
                    engagement_id="root-1",
                )
            )
        tool = LedgerTool(lambda: ledger, lambda: "root-1")
        requests, pages = [], []
        offset = 0
        while True:
            params = {"op": "profile", "entry_id": entry_id, "offset": offset}
            page = _run(tool.execute(params))
            self.assertTrue(page.success, page.error)
            requests.append(params)
            pages.append(page)
            offset = json.loads(page.output)["next_offset"]
            if offset is None:
                break
        self.assertGreater(len(pages), 1)
        for kb_state in ("working", "disabled", "failed"):
            with self.subTest(kb_state=kb_state):
                provider = _make_provider(
                    [
                        *[_tool_response("ledger", params) for params in requests],
                        LLMResponse(content="", model="t"),
                    ]
                )
                session = self._session(provider, [tool], threshold=32)
                session.tool_output_max_chars = 64
                if kb_state == "disabled":
                    session.knowledge_base = None
                elif kb_state == "failed":
                    session.knowledge_base = MagicMock()
                    session.knowledge_base.root_engagement_id = "root-1"
                    session.knowledge_base.add_artifact = AsyncMock(
                        side_effect=RuntimeError("disk full")
                    )
                _run(ConversationEngine().run_loop(session))
                messages = [m for m in session.history if m.role == "tool"]
                self.assertEqual(len(messages), len(pages))
                for message, page in zip(messages, pages):
                    self.assertEqual(
                        message.content, format_tool_result("ledger", page)
                    )

    def test_read_url_pages_stay_complete_in_history_and_are_still_indexed(self):
        url = "https://example.com/article"
        text = "".join(f"Paragraph {i:04d}: readable text.\n" for i in range(1000))
        tool = ReadUrlTool(config=ConfigManager())
        with (
            patch.object(tool, "_fetch_html", AsyncMock(return_value=("<html/>", url))),
            patch(
                "kerkov.tools.read_url_tool._extract_content",
                return_value=("Article", text),
            ),
        ):
            first_params = {"url": url}
            first = _run(tool.execute(first_params))
            next_offset = first.metadata["next_offset"]
            second_params = {"url": url, "offset": next_offset}
            second = _run(tool.execute(second_params))
            provider = _make_provider(
                [
                    _tool_response("read_url", first_params),
                    _tool_response("read_url", second_params),
                    LLMResponse(content="", model="t"),
                ]
            )
            session = self._session(provider, [tool])

            _run(ConversationEngine().run_loop(session))

        self.assertTrue(first.metadata["truncated"])
        self.assertFalse(second.metadata["truncated"])
        messages = [message for message in session.history if message.role == "tool"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(_run(self.kb.count()), 2)
        for artifact_id, (message, page) in enumerate(
            zip(messages, (first, second)), 1
        ):
            with self.subTest(artifact_id=artifact_id):
                self.assertGreater(len(page.output), 8000)
                self.assertEqual(message.content, format_tool_result("read_url", page))
                self.assertEqual(message.artifact_ref, artifact_id)
                stored = _run(self.kb.get_artifact(artifact_id))
                self.assertEqual(stored.body, page.output)
        first_body = split_tool_output(messages[0].content)[1]
        second_body = split_tool_output(messages[1].content)[1]
        self.assertEqual(first_body[:next_offset] + second_body, text)

    def test_read_url_page_survives_low_output_limits_without_working_kb(self):
        url = "https://example.com/article"
        text = "".join(f"Paragraph {i:04d}: readable text.\n" for i in range(1000))
        tool = ReadUrlTool(config=ConfigManager())
        params = {"url": url}
        with (
            patch.object(tool, "_fetch_html", AsyncMock(return_value=("<html/>", url))),
            patch(
                "kerkov.tools.read_url_tool._extract_content",
                return_value=("Article", text),
            ),
        ):
            page = _run(tool.execute(params))
            self.assertTrue(page.metadata["truncated"])
            for kb_fails in (False, True):
                with self.subTest(kb_fails=kb_fails):
                    provider = _make_provider(
                        [
                            _tool_response("read_url", params),
                            LLMResponse(content="", model="t"),
                        ]
                    )
                    session = _make_session(provider, tools=[tool])
                    session.tool_output_max_chars = 64
                    session.tool_output_head_chars = 16
                    session.tool_output_tail_chars = 8
                    session.kb_clip_threshold_bytes = 32
                    if kb_fails:
                        session.knowledge_base = MagicMock()
                        session.knowledge_base.root_engagement_id = "root-1"
                        session.knowledge_base.add_artifact = AsyncMock(
                            side_effect=RuntimeError("disk full")
                        )

                    _run(ConversationEngine().run_loop(session))

                    message = session.history[1]
                    self.assertEqual(
                        message.content, format_tool_result("read_url", page)
                    )
                    self.assertIsNone(message.artifact_ref)
                    if kb_fails:
                        session.knowledge_base.add_artifact.assert_awaited_once()
                        self.assertEqual(
                            session.knowledge_base.add_artifact.call_args.kwargs[
                                "body"
                            ],
                            page.output,
                        )

    def test_the_full_body_is_recoverable_from_the_store_after_clipping(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_BigEchoTool()], threshold=1000)

        _run(ConversationEngine().run_loop(session))
        stored = _run(self.kb.get_artifact(1))

        self.assertIn("line 1000 tomcat", stored.body)
        self.assertNotIn("line 1000 tomcat", session.history[1].content)

    def test_clipping_shrinks_what_reaches_history(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 5000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_BigEchoTool()], threshold=1000)

        _run(ConversationEngine().run_loop(session))

        stored = _run(self.kb.get_artifact(1))
        self.assertLess(len(session.history[1].content), len(stored.body) // 10)

    def test_output_exactly_at_the_threshold_is_not_clipped(self):
        provider = _make_provider(
            [
                _tool_response("echo", {"text": "x" * 20}),
                LLMResponse(content="", model="t"),
            ]
        )
        # "echo:" + 20 x's = 25 bytes of output.
        session = self._session(provider, [_EchoTool()], threshold=25)

        _run(ConversationEngine().run_loop(session))

        self.assertNotIn("artifact#", session.history[1].content)

    def test_bulky_metadata_is_clipped_out_of_history_too(self):
        # HTTPTool parks a parsed copy of the body in metadata, and
        # format_tool_result renders metadata into history — so clipping the
        # output alone would put the whole response straight back.
        provider = _make_provider(
            [
                _tool_response("http", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_ParsedJsonTool()], threshold=1000)

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn("artifact#1", content)
        self.assertIn("of 'parsed' kept out of this conversation", content)
        # A record the output preview elides must not survive in metadata.
        self.assertNotIn("tomcat-1000", content)
        # Small metadata values are still useful context and stay verbatim.
        self.assertIn("'status': 200", content)
        self.assertLess(len(content), len(_run(self.kb.get_artifact(1)).body) // 2)

    def test_metadata_counts_toward_the_clip_threshold(self):
        provider = _make_provider(
            [
                _tool_response("http", {"lines": 200}),
                LLMResponse(content="", model="t"),
            ]
        )
        # The body alone is under the threshold; body plus its metadata copy
        # is not, and history pays for both.
        body_bytes = len(json.dumps([f"tomcat-{i}" for i in range(200)]))
        session = self._session(
            provider, [_ParsedJsonTool()], threshold=body_bytes + 100
        )

        _run(ConversationEngine().run_loop(session))

        self.assertIn("artifact#1", session.history[1].content)
        self.assertIn(
            "of 'parsed' kept out of this conversation", session.history[1].content
        )

    def test_no_knowledge_base_normalizes_history_inline(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = _make_session(provider, tools=[_BigEchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertNotIn("artifact#", session.history[1].content)
        self.assertIn("clipped", session.history[1].content)
        self.assertIn("line 0 tomcat", session.history[1].content)
        self.assertNotIn("line 1000 tomcat", session.history[1].content)
        self.assertIn("line 1999 tomcat", session.history[1].content)

    def test_normalization_adds_reference_below_or_without_kb_clip_threshold(self):
        for threshold in (100_000, None):
            with self.subTest(threshold=threshold):
                provider = _make_provider(
                    [
                        _tool_response("bigecho", {"lines": 2000}),
                        LLMResponse(content="", model="t"),
                    ]
                )
                session = self._session(provider, [_BigEchoTool()], threshold=threshold)

                _run(ConversationEngine().run_loop(session))

                message = session.history[1]
                self.assertIn(f"artifact#{message.artifact_ref}", message.content)
                self.assertIn("clipped", message.content)
                self.assertNotIn("line 1000 tomcat", message.content)
                artifact = _run(self.kb.get_artifact(message.artifact_ref))
                expected = "\n".join(f"line {i} tomcat" for i in range(2000))
                self.assertEqual(artifact.body, expected)

    def test_repeated_output_is_stored_raw_and_collapsed_in_preview(self):
        text = "connection refused\n" * 1000
        provider = _make_provider(
            [_tool_response("echo", {"text": text}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [_EchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(_run(self.kb.get_artifact(1)).body, "echo:" + text)
        self.assertIn("similar lines omitted", session.history[1].content)
        self.assertIn("artifact#1", session.history[1].content)
        self.assertLess(len(session.history[1].content), 1000)

    def test_metadata_cannot_reintroduce_full_output_without_kb(self):
        provider = _make_provider(
            [
                _tool_response("http", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = _make_session(provider, tools=[_ParsedJsonTool()])

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertNotIn("tomcat-1000", content)
        self.assertNotIn("artifact#", content)
        self.assertIn("'status': 200", content)
        self.assertIn("'parsed'", content)
        self.assertLess(len(content), 6500)

    def test_custom_limits_only_change_history_copy(self):
        raw = "HEAD" + "x" * 500 + "CONSOLE_ONLY" + "x" * 6500 + "TAIL"
        original = ToolResult(success=True, output=raw)
        tool = _EchoTool()
        tool.execute = AsyncMock(return_value=original)
        provider = _make_provider(
            [_tool_response("echo", {"text": "go"}), LLMResponse(content="", model="t")]
        )
        console_buffer = io.StringIO()
        sink = ConsoleSink(
            Console(file=console_buffer, width=120, force_terminal=False)
        )
        sink.tool_succeeded = MagicMock(wraps=sink.tool_succeeded)
        session = _make_session(provider, tools=[tool], output=sink)
        session.tool_output_max_chars = 300
        session.tool_output_head_chars = 80
        session.tool_output_tail_chars = 40
        session.terminate_after_tool = MagicMock(return_value=False)
        session.engagement_store = MagicMock()

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn(raw[:80], content)
        self.assertTrue(content.endswith(raw[-40:]))
        self.assertIn(str(len(raw)), content)
        self.assertNotIn("CONSOLE_ONLY", content)
        sink.tool_succeeded.assert_called_once_with("echo", original)
        self.assertIs(session.terminate_after_tool.call_args.args[1], original)
        rendered_console = console_buffer.getvalue()
        self.assertIn("CONSOLE_ONLY", rendered_console)
        self.assertNotIn("TAIL", rendered_console)
        self.assertNotIn("clipped", rendered_console)
        self.assertEqual(original.output, raw)
        self.assertIn(
            session.history[1],
            [
                call.args[0]
                for call in session.engagement_store.append_message.call_args_list
            ],
        )

    def test_large_failed_tool_output_is_normalized_even_when_kb_write_fails(self):
        raw = "failure details\n" + "x" * 7000 + "\nlast error"
        original = ToolResult(
            success=False,
            output=raw,
            error="failed",
            metadata={"parsed": raw, "status": 500},
        )
        tool = _EchoTool()
        tool.execute = AsyncMock(return_value=original)
        provider = _make_provider(
            [_tool_response("echo", {"text": "go"}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [tool])
        session.knowledge_base = MagicMock()
        session.knowledge_base.add_artifact = AsyncMock(
            side_effect=RuntimeError("disk full")
        )

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn("Status: Failed", content)
        self.assertIn("Error: failed", content)
        self.assertIn("failure details", content)
        self.assertIn("last error", content)
        self.assertIn("clipped", content)
        self.assertNotIn("artifact#", content)
        self.assertIsNone(session.history[1].artifact_ref)
        self.assertLess(len(content), len(raw))
        session.knowledge_base.add_artifact.assert_awaited_once()
        self.assertEqual(
            session.knowledge_base.add_artifact.call_args.kwargs["body"], raw
        )

    def test_recall_output_is_not_re_indexed(self):
        # Otherwise the store fills with copies of its own answers, which then
        # compete with the originals for rank.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(success=True, output="recalled: " + "x" * 10_000)

            def approval_scope(self, params):
                return ApprovalScope.exact_only()

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("recall", {"text": "q"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FakeRecall()], threshold=100)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(_run(self.kb.count()), 0)
        self.assertNotIn("artifact#", session.history[1].content)
        self.assertEqual(
            split_tool_output(session.history[1].content)[1],
            "recalled: " + "x" * 10_000,
        )

    def test_recall_stamps_the_artifact_it_read_back(self):
        # Not re-indexed, but still stamped: compaction evicts an unstamped
        # tool result to an unrecoverable clip, a stamped one to its handle.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(
                    success=True,
                    output="recalled body",
                    metadata={"artifact_id": 42},
                )

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("recall", {"text": "q"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FakeRecall()], threshold=100)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(_run(self.kb.count()), 0)
        self.assertEqual(session.history[1].artifact_ref, 42)

    def test_recall_over_several_hits_is_not_stamped(self):
        # Ambiguous: the body is a concatenation, so no single handle recovers it.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(
                    success=True,
                    output="two bodies",
                    metadata={"hits": [{"artifact_id": 1}, {"artifact_id": 2}]},
                )

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("recall", {"text": "q"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FakeRecall()], threshold=100)

        _run(ConversationEngine().run_loop(session))

        self.assertIsNone(session.history[1].artifact_ref)

    def test_recall_stamps_the_artifact_it_read_back(self):
        # Not re-indexed, but still stamped: compaction evicts an unstamped
        # tool result to an unrecoverable clip, a stamped one to its handle.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(
                    success=True,
                    output="recalled body",
                    metadata={"artifact_id": 42},
                )

            def approval_scope(self, params):
                return ApprovalScope.exact_only()

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("recall", {"text": "q"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FakeRecall()], threshold=100)

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(_run(self.kb.count()), 0)
        self.assertEqual(session.history[1].artifact_ref, 42)

    def test_recall_over_several_hits_is_not_stamped(self):
        # Ambiguous: the body is a concatenation, so no single handle recovers it.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(
                    success=True,
                    output="two bodies",
                    metadata={"hits": [{"artifact_id": 1}, {"artifact_id": 2}]},
                )

            def approval_scope(self, params):
                return ApprovalScope.exact_only()

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("recall", {"text": "q"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FakeRecall()], threshold=100)

        _run(ConversationEngine().run_loop(session))

        self.assertIsNone(session.history[1].artifact_ref)

    def test_ingestion_failure_does_not_break_the_tool_call(self):
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [_EchoTool()])
        session.knowledge_base = MagicMock()
        session.knowledge_base.root_engagement_id = "root-1"
        session.knowledge_base.add_artifact = AsyncMock(
            side_effect=RuntimeError("disk full")
        )

        _run(ConversationEngine().run_loop(session))

        self.assertIn("echo:hi", session.history[1].content)

    def test_artifacts_are_attributed_to_the_writing_engagement_and_agent(self):
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        store = MagicMock()
        store.engagement_id = "agent-a"
        session = self._session(provider, [_EchoTool()])
        session.engagement_store = store
        session.kb_agent_type = "recon"

        _run(ConversationEngine().run_loop(session))
        hit = _run(self.kb.search("echo"))[0]

        # This is the row the master later reaches with
        # recall(engagement="agent-a").
        self.assertEqual(hit.engagement_id, "agent-a")
        self.assertEqual(hit.agent_type, "recon")

    def test_falls_back_to_the_root_id_when_there_is_no_engagement_store(self):
        provider = _make_provider(
            [_tool_response("echo", {"text": "hi"}), LLMResponse(content="", model="t")]
        )
        session = self._session(provider, [_EchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertEqual(_run(self.kb.search("echo"))[0].engagement_id, "root-1")

    def test_failed_tool_output_is_indexed_too(self):
        class _FailingTool(BaseTool):
            def __init__(self) -> None:
                super().__init__("failing", "always fails")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(
                    success=False,
                    output="nmap: Failed to resolve tomcat.local",
                    error="exit 1",
                )

            def approval_scope(self, params):
                return ApprovalScope.exact_only()

            def validate_params(self, toolParams) -> Optional[str]:
                return None

        provider = _make_provider(
            [
                _tool_response("failing", {"text": "x"}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_FailingTool()])

        _run(ConversationEngine().run_loop(session))
        hits = _run(self.kb.search("resolve"))

        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].metadata["success"])


class ArtifactTitleTest(unittest.TestCase):
    def test_command_is_preferred_for_the_title(self):
        call = _tool_call("exec", {"command": "nmap -sV 10.0.0.5", "timeout": 60})

        self.assertEqual(artifact_title(call), "exec: nmap -sV 10.0.0.5")

    def test_url_is_used_when_there_is_no_command(self):
        call = _tool_call("http", {"method": "GET", "url": "http://a/b"})

        self.assertEqual(artifact_title(call), "http: http://a/b")

    def test_long_values_are_truncated(self):
        call = _tool_call("exec", {"command": "x" * 200})

        self.assertTrue(artifact_title(call).endswith("..."))
        self.assertLess(len(artifact_title(call)), 100)

    def test_falls_back_to_the_tool_name(self):
        self.assertEqual(artifact_title(_tool_call("browser", {"n": 1})), "browser")


class ArtifactKindForToolTest(unittest.TestCase):
    def test_read_and_http_get_specific_kinds(self):
        self.assertEqual(artifact_kind_for_tool("read"), ArtifactKind.FILE)
        self.assertEqual(artifact_kind_for_tool("http"), ArtifactKind.HTTP_RESPONSE)

    def test_everything_else_falls_back_to_tool_output(self):
        for name in ("exec", "browser", "cve_lookup", "delegate"):
            with self.subTest(tool=name):
                self.assertEqual(artifact_kind_for_tool(name), ArtifactKind.TOOL_OUTPUT)


class BuildClippedMetadataTest(unittest.TestCase):
    def test_small_values_are_left_alone(self):
        metadata = {"status": 200, "url": "http://a/b"}

        self.assertEqual(build_clipped_metadata(metadata, 3), metadata)

    def test_oversized_values_become_a_reference(self):
        clipped = build_clipped_metadata({"parsed": ["x" * 100] * 50}, 3)

        self.assertIn("artifact#3", clipped["parsed"])
        self.assertNotIn("xxxx", clipped["parsed"])

    def test_empty_metadata_is_passed_through(self):
        self.assertIsNone(build_clipped_metadata(None, 1))
        self.assertEqual(build_clipped_metadata({}, 1), {})


class HistoryFootprintTest(unittest.TestCase):
    def test_metadata_is_counted_with_the_output(self):
        without = ToolResult(success=True, output="body")
        with_metadata = ToolResult(
            success=True, output="body", metadata={"parsed": "z" * 500}
        )

        self.assertEqual(history_footprint_bytes(without), 4)
        self.assertGreater(history_footprint_bytes(with_metadata), 500)


class SummarizeParametersTest(unittest.TestCase):
    def test_large_string_parameters_are_truncated(self):
        summary = _summarize_parameters({"body": "z" * 5000, "method": "POST"})

        self.assertTrue(summary["body"].endswith("..."))
        self.assertLess(len(summary["body"]), 300)
        self.assertEqual(summary["method"], "POST")

    def test_none_is_handled(self):
        self.assertEqual(_summarize_parameters(None), {})


class FormatSecondsTest(unittest.TestCase):
    def test_sub_minute(self) -> None:
        self.assertEqual(_format_seconds(5.0), "5s")
        self.assertEqual(_format_seconds(59.9), "59s")

    def test_whole_minutes(self) -> None:
        self.assertEqual(_format_seconds(60.0), "1m0s")
        self.assertEqual(_format_seconds(120.0), "2m0s")

    def test_boundary_float_no_invalid_60s(self) -> None:
        # Regression: 119.6 previously returned "1m60s"
        self.assertEqual(_format_seconds(119.6), "1m59s")
        # Regression: 3599.6 previously returned "59m60s"
        self.assertEqual(_format_seconds(3599.6), "59m59s")

    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(_format_seconds(90.0), "1m30s")
        self.assertEqual(_format_seconds(3661.0), "61m1s")


class LedgerInjectionTest(unittest.TestCase):
    """The ledger block is injected transiently and never persisted to history."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        self.ledger = FindingsLedger(self.kb)
        self.engine = ConversationEngine()

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_flag(self, value: str) -> None:
        _run(
            self.ledger.add(
                LedgerSection.FLAGS, {"value": value}, engagement_id="root-1"
            )
        )

    def test_block_injected_after_leading_system_prompt(self):
        self._add_flag("HTB{x}")
        history = [
            Message(role="system", content="base prompt"),
            Message(role="user", content="hi"),
        ]
        session = _make_session(_make_provider([]), history=history)
        session.ledger = self.ledger
        _run(self.engine._refresh_ledger_block(session))

        messages = self.engine._messages_for_provider(session)

        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].content, "base prompt")
        self.assertEqual(messages[1].role, "system")
        self.assertIn("HTB{x}", messages[1].content)
        # Never written into the persisted history.
        self.assertTrue(all("HTB{x}" not in m.content for m in session.history))

    def test_empty_ledger_injects_nothing(self):
        history = [Message(role="system", content="base prompt")]
        session = _make_session(_make_provider([]), history=history)
        session.ledger = self.ledger
        _run(self.engine._refresh_ledger_block(session))

        messages = self.engine._messages_for_provider(session)

        self.assertEqual(len(messages), 1)
        self.assertIsNone(session.ledger_block)

    def test_connection_policy_applies_to_empty_ledger_and_custom_agent_prompts(self):
        for cache_prefix in (False, True):
            with self.subTest(cache_prefix=cache_prefix):
                provider = _make_provider([])
                provider.caches_prompt_prefix = cache_prefix
                history = [
                    Message(role="system", content="custom agent"),
                    Message(role="user", content="SSH to host"),
                ]
                session = _make_session(
                    provider,
                    history=history,
                    tools=[LedgerTool(lambda: self.ledger, lambda: "root-1")],
                )
                session.ledger = self.ledger
                first = self.engine._messages_for_provider(session)
                second = self.engine._messages_for_provider(session)
                self.assertEqual(first, second)
                self.assertIn(
                    "Automatically record successful host connections", first[0].content
                )
                self.assertIn("a password prompt", first[0].content)
                self.assertEqual(history[0].content, "custom agent")
                self.assertEqual(len(history), 2)

    def test_connection_policy_requires_both_ledger_and_write_tool(self):
        session = _make_session(
            _make_provider([]),
            tools=[LedgerTool(lambda: self.ledger, lambda: "root-1")],
        )
        self.assertEqual(self.engine._messages_for_provider(session), [])
        session.ledger = self.ledger
        session.tools = []
        self.assertEqual(self.engine._messages_for_provider(session), [])

    def test_refresh_reflects_a_write_made_this_turn(self):
        session = _make_session(_make_provider([]), history=[])
        session.ledger = self.ledger

        _run(self.engine._refresh_ledger_block(session))
        self.assertIsNone(session.ledger_block)

        self._add_flag("HTB{y}")
        _run(self.engine._refresh_ledger_block(session))
        self.assertIsNotNone(session.ledger_block)
        self.assertIn("HTB{y}", session.ledger_block)

    def test_block_rides_at_tail_for_prefix_caching_provider(self):
        self._add_flag("HTB{x}")
        history = [
            Message(role="system", content="base prompt"),
            Message(role="user", content="hi"),
        ]
        provider = _make_provider([])
        provider.caches_prompt_prefix = True
        session = _make_session(provider, history=history)
        session.ledger = self.ledger
        _run(self.engine._refresh_ledger_block(session))

        messages = self.engine._messages_for_provider(session)

        # Base prompt stays first; the mutable ledger rides as the final message
        # so the whole system+history prefix stays cacheable.
        self.assertEqual(messages[0].content, "base prompt")
        self.assertEqual(messages[1].content, "hi")
        self.assertEqual(messages[-1].role, "system")
        self.assertIn("HTB{x}", messages[-1].content)
        self.assertTrue(all("HTB{x}" not in m.content for m in session.history))

    def test_tail_ledger_follows_transient_prompts(self):
        self._add_flag("HTB{x}")
        provider = _make_provider([])
        provider.caches_prompt_prefix = True
        session = _make_session(
            provider,
            history=[Message(role="user", content="hi")],
            transient_prompts=["NUDGE"],
        )
        session.ledger = self.ledger
        _run(self.engine._refresh_ledger_block(session))

        messages = self.engine._messages_for_provider(session)

        self.assertEqual(messages[-2].content, "NUDGE")
        self.assertIn("HTB{x}", messages[-1].content)

    def test_credential_survives_compaction(self):
        """Headline acceptance: a ledger credential outlives history summarization."""
        _run(
            self.ledger.add(
                LedgerSection.CREDENTIALS,
                {"username": "svc_sql", "secret": "S3cr3t!", "kind": "password"},
                engagement_id="root-1",
            )
        )
        tracker = TokenTracker()
        compactor = ContextCompactor(
            token_tracker=tracker,
            max_context_tokens=1000,
            threshold_pct=0.65,
            summary_reserve_tokens=200,
        )
        history = [Message(role="system", content="base")]
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            history.append(Message(role=role, content=f"msg {i}"))

        async def summarize(_prompt: str) -> str:
            return "compacted narrative without the credential"

        new_history = _run(compactor.compact(history, summarize_fn=summarize))
        self.assertIsNotNone(new_history)
        # The credential is gone from the compacted history...
        self.assertTrue(all("S3cr3t!" not in m.content for m in new_history))

        # ...but still injected at the top of context from the ledger.
        session = _make_session(_make_provider([]), history=new_history)
        session.ledger = self.ledger
        _run(self.engine._refresh_ledger_block(session))
        messages = self.engine._messages_for_provider(session)
        self.assertTrue(any("S3cr3t!" in m.content for m in messages))


class StreamOutputToggleTest(unittest.TestCase):
    """`STREAM_OUTPUT` flows into `ConsoleSink.supports_streaming` and gates deltas."""

    def _create_console(self) -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=True,
            width=80,
            height=24,
            color_system=None,
            legacy_windows=False,
            _environ={"TERM": "xterm-256color"},
        )

    def test_supports_streaming_reflects_stream_flag(self):
        console = self._create_console()
        self.assertTrue(ConsoleSink(console).supports_streaming)
        self.assertTrue(ConsoleSink(console, stream=True).supports_streaming)
        self.assertFalse(ConsoleSink(console, stream=False).supports_streaming)

    def test_engine_passes_no_delta_when_streaming_off(self):
        provider = _make_provider([LLMResponse(content="# Title", model="test")])
        sink = ConsoleSink(self._create_console(), stream=False)
        session = _make_session(
            provider, history=[Message(role="user", content="hi")], output=sink
        )

        _run(ConversationEngine().run_loop(session))

        self.assertIsNone(provider.complete.await_args.kwargs["on_delta"])

    def test_engine_threads_delta_when_streaming_on(self):
        provider = _make_provider([LLMResponse(content="hi", model="test")])
        sink = ConsoleSink(self._create_console(), stream=True)
        session = _make_session(
            provider, history=[Message(role="user", content="hi")], output=sink
        )

        _run(ConversationEngine().run_loop(session))

        self.assertIsNotNone(provider.complete.await_args.kwargs["on_delta"])

    def test_final_renders_markdown_when_streaming_off(self):
        console = self._create_console()
        sink = ConsoleSink(console, stream=False)

        sink.final("# Heading\n\nbody text")

        output = console.file.getvalue()
        self.assertIn("Heading", output)
        # Markdown() renders a heading as a boxed/ruled block, never the raw '#'.
        self.assertNotIn("# Heading", output)

    def test_streamed_content_is_not_rerendered_when_streaming_on(self):
        console = self._create_console()
        sink = ConsoleSink(console, stream=True)

        sink.begin_assistant_stream()
        sink.assistant_delta("# Heading")
        sink.end_assistant_stream()
        marker = console.file.getvalue()
        sink.final("# Heading")

        # `final` matches what was streamed and prints nothing further.
        self.assertEqual(console.file.getvalue(), marker)
        self.assertIn("# Heading", marker)

    def test_empty_partial_interrupt_with_streaming_off_does_not_raise(self):
        from kerkov.core.cancellable_task import InterruptedByUser

        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=InterruptedByUser)
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        sink = ConsoleSink(self._create_console(), stream=False)
        session = _make_session(
            provider, history=[Message(role="user", content="hi")], output=sink
        )

        result = _run(ConversationEngine().run_loop(session))

        self.assertIsNone(result)
        self.assertFalse(any(m.role == "assistant" for m in session.history))


if __name__ == "__main__":
    unittest.main()
