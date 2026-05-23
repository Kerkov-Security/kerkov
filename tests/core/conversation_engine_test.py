"""Tests for ConversationEngine — the extracted shared tool-execution loop."""

import asyncio
import unittest
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.context.context_compactor import ContextCompactor
from src.context.token_tracker import TokenTracker
from src.core.conversation_engine import (
    ConversationEngine,
    LoopSession,
    OutputSink,
    QuietSink,
    TurnTimer,
    WhitelistAutoApproval,
    extract_raw_blocks,
    format_tool_result,
    looks_like_deferred_execution,
)
from src.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from src.tools.base_tool import (
    BaseTool,
    ToolApprovalLevel,
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

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.NONE

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
                    usage=TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
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
                from src.cli.tool_approval_helper import ToolApprovalResult

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
        from src.cli.cancellable_task import InterruptedByUser

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

        session = _make_session(
            provider, tools=[_EchoTool()], output=_TrackingSink()
        )
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


if __name__ == "__main__":
    unittest.main()
