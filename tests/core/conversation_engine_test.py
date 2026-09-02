"""Tests for ConversationEngine — the extracted shared tool-execution loop."""

import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console

from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.token_tracker import TokenTracker
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
    artifact_kind_for_tool,
    artifact_title,
    build_clipped_metadata,
    build_clipped_output,
    extract_raw_blocks,
    format_tool_result,
    history_footprint_bytes,
    looks_like_deferred_execution,
)
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import ArtifactKind, LedgerSection
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)


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

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def _make_provider(responses: List[LLMResponse]) -> MagicMock:
    """Mock provider whose `complete()` returns the queued responses in order."""
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)
    provider.get_max_output_tokens = AsyncMock(return_value=2048)
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)
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
        provider = _make_provider(
            [
                LLMResponse(content="I'll do that next.", model="test"),
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
        from kerkov.cli.cancellable_task import InterruptedByUser

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

    def test_large_output_is_replaced_by_a_reference_with_a_preview(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_BigEchoTool()], threshold=1000)

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn("artifact#1", content)
        self.assertIn("recall(query='artifact#1')", content)
        self.assertIn("characters elided", content)
        # Head and tail previews both survive, so the model can judge the blob.
        self.assertIn("line 0 tomcat", content)
        self.assertIn("line 1999 tomcat", content)

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
                _tool_response("http", {"lines": 500}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = self._session(provider, [_ParsedJsonTool()], threshold=1000)

        _run(ConversationEngine().run_loop(session))

        content = session.history[1].content
        self.assertIn("artifact#1", content)
        self.assertIn("of 'parsed' kept out of this conversation", content)
        # A record the output preview elides must not survive in metadata.
        self.assertNotIn("tomcat-250", content)
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

    def test_no_knowledge_base_leaves_history_untouched(self):
        provider = _make_provider(
            [
                _tool_response("bigecho", {"lines": 2000}),
                LLMResponse(content="", model="t"),
            ]
        )
        session = _make_session(provider, tools=[_BigEchoTool()])

        _run(ConversationEngine().run_loop(session))

        self.assertNotIn("artifact#", session.history[1].content)
        self.assertIn("line 1999 tomcat", session.history[1].content)

    def test_recall_output_is_not_re_indexed(self):
        # Otherwise the store fills with copies of its own answers, which then
        # compete with the originals for rank.
        class _FakeRecall(BaseTool):
            def __init__(self) -> None:
                super().__init__("recall", "fake recall")

            def get_params_class(self):
                return _EchoParams

            async def run(self, toolParams) -> ToolResult:
                return ToolResult(success=True, output="recalled: " + "x" * 5000)

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


class BuildClippedOutputTest(unittest.TestCase):
    def test_short_enough_output_has_no_tail_section(self):
        clipped = build_clipped_output("short output", 7)

        self.assertIn("artifact#7", clipped)
        self.assertIn("short output", clipped)
        self.assertNotIn("elided", clipped)

    def test_reference_names_the_retrieval_call(self):
        clipped = build_clipped_output("y" * 50_000, 42)

        self.assertIn("recall(query='artifact#42')", clipped)
        self.assertIn("elided", clipped)

    def test_size_is_reported_in_human_units(self):
        self.assertIn("KB", build_clipped_output("y" * 50_000, 1))
        self.assertIn("MB", build_clipped_output("y" * 2_000_000, 1))


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

    def test_refresh_reflects_a_write_made_this_turn(self):
        session = _make_session(_make_provider([]), history=[])
        session.ledger = self.ledger

        _run(self.engine._refresh_ledger_block(session))
        self.assertIsNone(session.ledger_block)

        self._add_flag("HTB{y}")
        _run(self.engine._refresh_ledger_block(session))
        self.assertIsNotNone(session.ledger_block)
        self.assertIn("HTB{y}", session.ledger_block)

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


if __name__ == "__main__":
    unittest.main()
