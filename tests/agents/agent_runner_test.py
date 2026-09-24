"""Tests for AgentRunner — isolated agent loop + structured report."""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.models import AgentProfile
from kerkov.agents.report_tool import REPORT_TOOL_NAME, ReportTool
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.conversation_engine import ConversationEngine, WhitelistAutoApproval
from kerkov.core.run_workspace import get_current_workspace
from kerkov.models.basic_models import LLMResponse, TokenUsage, ToolCall
from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)


def _run(coro):
    return asyncio.run(coro)


class _EchoParams(ToolParams):
    text: str = ""


class _EchoTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("echo", "echo")

    def get_params_class(self):
        return _EchoParams

    async def run(self, toolParams: _EchoParams) -> ToolResult:
        return ToolResult(success=True, output=f"echo:{toolParams.text}")

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams) -> Optional[str]:
        return None


def _provider(responses: List[LLMResponse]) -> MagicMock:
    p = MagicMock()
    p.model = "test-model"
    p.complete = AsyncMock(side_effect=responses)
    p.get_max_output_tokens = AsyncMock(return_value=2048)
    p.get_max_context_tokens = AsyncMock(return_value=100_000)
    return p


def _profile(name: str = "test", tools: Optional[List[str]] = None) -> AgentProfile:
    return AgentProfile(
        name=name,
        description="A test profile",
        system_prompt="You are a test agent.",
        allowed_tools=tools or ["echo"],
        max_iterations=5,
    )


def _report_call(status: str, findings: str, next_steps: str = "") -> ToolCall:
    return ToolCall(
        tool_name=REPORT_TOOL_NAME,
        parameters={
            "status": status,
            "findings": findings,
            "next_steps": next_steps,
        },
        call_id="r1",
    )


def _report_response(findings: str = "done") -> LLMResponse:
    return LLMResponse(
        content="",
        model="test",
        tool_calls=[_report_call("completed", findings)],
    )


class ReportTerminatesLoopTest(unittest.TestCase):
    def test_report_call_ends_run_and_returns_payload(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        _report_call("completed", "found the flag", "patch it")
                    ],
                )
            ]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="find the flag",
            provider=provider,
            tools=[_EchoTool()],
        )

        report = _run(runner.run())

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.findings, "found the flag")
        self.assertEqual(report.next_steps, "patch it")
        self.assertEqual(report.agent_type, "test")
        # One LLM call — the agent called `report` immediately.
        self.assertEqual(provider.complete.await_count, 1)

    def test_report_call_does_not_write_tool_parameters_to_stdout(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "sensitive finding")],
                )
            ]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="find the flag",
            provider=provider,
            tools=[_EchoTool()],
        )
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            _run(runner.run())

        self.assertEqual(stdout.getvalue(), "")

    def test_tool_then_report(self):
        provider = _provider(
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
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "all done")],
                ),
            ]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="say hi",
            provider=provider,
            tools=[_EchoTool()],
        )

        report = _run(runner.run())

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.findings, "all done")
        self.assertEqual(provider.complete.await_count, 2)


class ToolOutputNormalizationTest(unittest.TestCase):
    def test_configured_limit_applies_without_a_knowledge_base(self):
        large_text = "START:" + "a" * 500 + "MIDDLE_SENTINEL" + "z" * 500 + ":END"
        # _EchoTool adds five characters to its output.
        boundary_text = "b" * 295
        config = SimpleNamespace(
            kb_clip_threshold_bytes=8000,
            tool_output_max_chars=300,
            tool_output_head_chars=80,
            tool_output_tail_chars=40,
            # Wide enough that the agent's compactor never fires: this test is
            # about the per-result normalization, not about reclaiming context.
            max_context_tokens=100_000,
            compaction_threshold_pct=0.65,
            summary_reserve_tokens=2000,
        )

        for text, should_clip in [(large_text, True), (boundary_text, False)]:
            with self.subTest(should_clip=should_clip):
                provider = _provider(
                    [
                        LLMResponse(
                            content="",
                            model="test",
                            tool_calls=[
                                ToolCall(
                                    tool_name="echo",
                                    parameters={"text": text},
                                    call_id="t1",
                                )
                            ],
                        ),
                        LLMResponse(
                            content="",
                            model="test",
                            tool_calls=[_report_call("completed", "output checked")],
                        ),
                    ]
                )
                session = AgentSession(
                    id="a1", agent_run_id="run1", profile="test", task="echo output"
                )
                with tempfile.TemporaryDirectory() as d:
                    store = EngagementStore(store_dir=Path(d) / "engagements")
                    runner = AgentRunner(
                        profile=_profile(),
                        instruction="echo output, then report",
                        provider=provider,
                        tools=[_EchoTool()],
                        engagement_store=store,
                        run_workspace=Path(d) / "workspace",
                        session=session,
                    )
                    with patch(
                        "kerkov.agents.agent_runner.get_config", return_value=config
                    ):
                        report = _run(runner.run())

                self.assertEqual(report.status, "completed")
                self.assertEqual(report.findings, "output checked")
                self.assertEqual(provider.complete.await_count, 2)
                tool_message = next(
                    message
                    for message in session.history
                    if message.role == "tool" and message.call_id == "t1"
                )
                self.assertIsNone(tool_message.artifact_ref)
                raw_output = f"echo:{text}"
                if should_clip:
                    self.assertIn(raw_output[:80], tool_message.content)
                    self.assertIn(raw_output[-40:], tool_message.content)
                    self.assertNotIn("MIDDLE_SENTINEL", tool_message.content)
                    self.assertIn("clipped", tool_message.content.lower())
                    self.assertIn(str(len(raw_output)), tool_message.content)
                    self.assertNotIn("recall(", tool_message.content)
                    self.assertLess(len(tool_message.content), len(raw_output))
                else:
                    self.assertTrue(tool_message.content.endswith(raw_output))
                    self.assertNotIn("clipped", tool_message.content.lower())


class AgentContextManagementTest(unittest.TestCase):
    """Issue #205 item 1: an agent with no compactor can only die of its context."""

    def test_the_agent_loop_gets_a_compactor_and_a_tracker(self):
        captured = {}
        provider = _provider([_report_response()])

        class _CapturingEngine(ConversationEngine):
            async def run_loop(self, session):
                captured["session"] = session
                return await super().run_loop(session)

        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=provider,
            tools=[_EchoTool()],
            engine=_CapturingEngine(),
        )
        _run(runner.run())

        session = captured["session"]
        self.assertIsNotNone(session.compactor)
        self.assertIsNotNone(session.token_tracker)

    def test_the_compactor_targets_the_agents_own_context_window(self):
        provider = _provider([_report_response()])
        provider.get_max_context_tokens = AsyncMock(return_value=42_000)
        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=provider,
            tools=[_EchoTool()],
        )

        config = SimpleNamespace(
            kb_clip_threshold_bytes=8000,
            tool_output_max_chars=6000,
            tool_output_head_chars=1500,
            tool_output_tail_chars=500,
            max_context_tokens=50_000,
            compaction_threshold_pct=0.65,
            summary_reserve_tokens=2000,
        )
        compactor = _run(runner._build_compactor(config))

        self.assertEqual(compactor.max_tokens, 42_000)
        self.assertEqual(compactor.threshold, int(42_000 * 0.65))

    def test_an_unknown_window_falls_back_to_the_configured_maximum(self):
        provider = _provider([_report_response()])
        provider.get_max_context_tokens = AsyncMock(return_value=None)
        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=provider,
            tools=[_EchoTool()],
        )

        config = SimpleNamespace(
            max_context_tokens=50_000,
            compaction_threshold_pct=0.65,
            summary_reserve_tokens=2000,
        )
        compactor = _run(runner._build_compactor(config))

        self.assertEqual(compactor.max_tokens, 50_000)

    def test_the_report_carries_what_the_run_billed(self):
        """The master's only view of fan-out spend: its own tracker never sees it."""
        response = _report_response()
        response.usage = TokenUsage(prompt_tokens=1200, completion_tokens=300)
        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=_provider([response]),
            tools=[_EchoTool()],
        )

        report = _run(runner.run())

        self.assertEqual(report.tokens_used, 1500)

    def test_spend_accumulates_across_calls_while_size_does_not(self):
        first = LLMResponse(
            content="",
            model="test",
            tool_calls=[ToolCall(tool_name="echo", parameters={}, call_id="e")],
            usage=TokenUsage(prompt_tokens=1000, completion_tokens=100),
        )
        second = _report_response()
        second.usage = TokenUsage(prompt_tokens=1400, completion_tokens=100)
        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=_provider([first, second]),
            tools=[_EchoTool()],
        )

        report = _run(runner.run())

        self.assertEqual(report.tokens_used, 2600)
        # The conversation itself is only ever as big as the last call said.
        self.assertEqual(runner.token_tracker.total_tokens, 1500)

    def test_a_long_agent_run_compacts_instead_of_growing_forever(self):
        """The whole point of item 1: the run reclaims its own context."""
        captured = {}
        summarized = []

        class _CapturingEngine(ConversationEngine):
            async def run_loop(self, session):
                captured["session"] = session
                return await super().run_loop(session)

        calls = {"n": 0}

        async def complete(**kwargs):
            # The summarizer calls the same provider without tools.
            if not kwargs.get("tools"):
                summarized.append(kwargs["messages"][0].content)
                return LLMResponse(content="earlier work, summarized", model="test")
            calls["n"] += 1
            if calls["n"] > 4:
                return _report_response()
            return LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name="echo",
                        parameters={"text": "x" * 200},
                        call_id=f"c{calls['n']}",
                    )
                ],
                # Over 65% of the 50k window the config below declares.
                usage=TokenUsage(prompt_tokens=40_000, completion_tokens=100),
            )

        provider = MagicMock()
        provider.model = "test-model"
        provider.complete = AsyncMock(side_effect=complete)
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        provider.get_max_context_tokens = AsyncMock(return_value=50_000)

        runner = AgentRunner(
            profile=_profile(name="test", tools=["echo"]),
            instruction="work",
            provider=provider,
            tools=[_EchoTool()],
            engine=_CapturingEngine(),
        )
        _run(runner.run())

        self.assertTrue(summarized, "the agent never compacted")
        history = captured["session"].history
        self.assertTrue(
            any(m.summary_level == 1 for m in history),
            "compaction left no summary in the agent's history",
        )

    def test_the_session_sees_the_tracker_while_the_run_is_live(self):
        session = AgentSession(
            id="a1", agent_run_id="run1", profile="test", task="work"
        )
        response = _report_response()
        response.usage = TokenUsage(prompt_tokens=800, completion_tokens=200)
        runner = AgentRunner(
            profile=_profile(),
            instruction="work",
            provider=_provider([response]),
            tools=[_EchoTool()],
            session=session,
        )

        self.assertEqual(session.tokens_used(), 0)
        _run(runner.run())
        self.assertEqual(session.tokens_used(), 1000)


class StuckPathsTest(unittest.TestCase):
    def test_max_iterations_returns_stuck(self):
        provider = MagicMock()
        provider.model = "test"
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name="echo",
                        parameters={"text": "x"},
                        call_id="c",
                    )
                ],
            )
        )
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        provider.get_max_context_tokens = AsyncMock(return_value=100_000)

        runner = AgentRunner(
            profile=_profile(),
            instruction="loop",
            provider=provider,
            tools=[_EchoTool()],
        )

        report = _run(runner.run())

        self.assertEqual(report.status, "stuck")
        # The runner allowed exactly max_iterations completions before giving up.
        self.assertEqual(provider.complete.await_count, 5)

    def test_configured_max_iterations_caps_profile_limit(self):
        provider = MagicMock()
        provider.model = "test"
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name="echo",
                        parameters={"text": "x"},
                        call_id="c",
                    )
                ],
            )
        )
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        provider.get_max_context_tokens = AsyncMock(return_value=100_000)

        runner = AgentRunner(
            profile=_profile(),
            instruction="loop",
            provider=provider,
            tools=[_EchoTool()],
            max_iterations=2,
        )

        report = _run(runner.run())

        self.assertEqual(report.status, "stuck")
        self.assertEqual(provider.complete.await_count, 2)

    def test_final_text_without_report_becomes_completed(self):
        provider = _provider([LLMResponse(content="here is the answer", model="test")])
        runner = AgentRunner(
            profile=_profile(),
            instruction="answer",
            provider=provider,
            tools=[],
        )

        report = _run(runner.run())

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.findings, "here is the answer")


class InitialHistoryTest(unittest.TestCase):
    def test_history_contains_system_prompt_and_instruction(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "ok")],
                )
            ]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="scan the box",
            success_criteria="report open ports",
            provider=provider,
            tools=[],
        )

        _run(runner.run())

        _, kwargs = provider.complete.call_args_list[0]
        messages = kwargs["messages"]
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].content, "You are a test agent.")
        self.assertEqual(messages[1].role, "user")
        self.assertIn("scan the box", messages[1].content)
        self.assertIn("report open ports", messages[1].content)


class ReportToolIsAlwaysAddedTest(unittest.TestCase):
    def test_report_tool_present_even_when_no_other_tools(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "done")],
                )
            ]
        )
        runner = AgentRunner(
            profile=_profile(tools=[]),
            instruction="nothing",
            provider=provider,
            tools=[],
        )

        _run(runner.run())

        _, kwargs = provider.complete.call_args_list[0]
        tool_names = {t.name for t in kwargs["tools"]}
        self.assertIn(REPORT_TOOL_NAME, tool_names)


class EngagementPersistenceTest(unittest.TestCase):
    def test_agent_run_writes_linked_engagement_file(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "ok")],
                )
            ]
        )
        with tempfile.TemporaryDirectory() as d:
            store = EngagementStore(store_dir=Path(d))
            runner = AgentRunner(
                profile=_profile(),
                instruction="hello world task",
                provider=provider,
                tools=[],
                engagement_store=store,
                parent_engagement_id="parentXYZ",
                parent_call_id="callABC",
            )

            report = _run(runner.run())

            self.assertIsNotNone(report.engagement_id)
            loaded = store.load_engagement(report.engagement_id)
            self.assertEqual(loaded.metadata.kind, "agent")
            self.assertEqual(loaded.metadata.parent_engagement_id, "parentXYZ")
            self.assertEqual(loaded.metadata.parent_call_id, "callABC")
            self.assertEqual(loaded.metadata.agent_type, "test")
            self.assertIn("hello world", loaded.metadata.task_instruction)


class SupervisedRunTest(unittest.TestCase):
    """What the supervisor injects, and what the runner writes back."""

    def _session(self) -> AgentSession:
        return AgentSession(
            id="a1", agent_run_id="run1", profile="test", task="do a thing"
        )

    def test_injected_workspace_is_used_instead_of_a_per_agent_one(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "ok")],
                )
            ]
        )
        with tempfile.TemporaryDirectory() as d:
            store = EngagementStore(store_dir=Path(d) / "engagements")
            shared = Path(d) / "shared-workspace"
            seen = {}

            class _WorkspaceProbe(BaseTool):
                def __init__(self) -> None:
                    super().__init__("echo", "probe the active workspace")

                def get_params_class(self):
                    return _EchoParams

                def approval_scope(self, params):
                    return ApprovalScope.exact_only()

                def validate_params(self, toolParams):
                    return None

                async def run(self, toolParams) -> ToolResult:
                    seen["workspace"] = get_current_workspace()
                    return ToolResult(success=True, output="ok")

            provider = _provider(
                [
                    LLMResponse(
                        content="",
                        model="test",
                        tool_calls=[
                            ToolCall(tool_name="echo", parameters={}, call_id="t1")
                        ],
                    ),
                    LLMResponse(
                        content="",
                        model="test",
                        tool_calls=[_report_call("completed", "ok")],
                    ),
                ]
            )
            runner = AgentRunner(
                profile=_profile(),
                instruction="probe",
                provider=provider,
                tools=[_WorkspaceProbe()],
                engagement_store=store,
                run_workspace=shared,
            )
            report = _run(runner.run())

            self.assertEqual(report.status, "completed")
            self.assertEqual(seen["workspace"], shared.resolve())
            # No directory was derived from the agent's own engagement id.
            self.assertFalse((Path(d) / "engagements" / report.engagement_id).is_dir())

    def test_the_session_receives_history_engagement_id_and_status(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(tool_name="echo", parameters={}, call_id="t1")
                    ],
                ),
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "ok")],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as d:
            store = EngagementStore(store_dir=Path(d))
            session = self._session()
            runner = AgentRunner(
                profile=_profile(),
                instruction="probe",
                provider=provider,
                tools=[_EchoTool()],
                engagement_store=store,
                session=session,
            )
            report = _run(runner.run())

        self.assertEqual(session.engagement_id, report.engagement_id)
        self.assertEqual(session.iteration, 2)
        self.assertEqual(session.state, AgentState.REPORTING)
        self.assertIsNone(session.current_tool)
        # The live transcript an attached view renders is the session's list.
        self.assertEqual(session.history[0].role, "system")
        self.assertTrue(any(m.role == "tool" for m in session.history))
        self.assertTrue(session.events.buffer)


class InterruptibleRunTest(unittest.TestCase):
    """A run the supervisor stopped reports `partial`, not `stuck`."""

    def _session(self) -> AgentSession:
        return AgentSession(
            id="a1", agent_run_id="run1", profile="test", task="do a thing"
        )

    def test_the_loop_session_gets_the_sessions_inbox_and_a_real_interrupt(self):
        from kerkov.cli.cancellable_task import EventInterrupt

        session = self._session()
        captured = {}

        class _CapturingEngine:
            async def run_loop(self, loop_session):
                captured["session"] = loop_session
                return None

        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=_provider([]),
            tools=[],
            session=session,
            engine=_CapturingEngine(),
        )
        _run(runner.run())

        built = captured["session"]
        self.assertIs(built.inbox, session.inbox)
        self.assertIs(built.interrupt, session.interrupt)
        self.assertIsInstance(built.interrupt, EventInterrupt)
        self.assertTrue(built.interrupt_ends_turn)

    def test_a_runner_without_a_session_keeps_the_uninterruptible_loop(self):
        """Direct `AgentRunner` use — no supervisor, no interrupt, no inbox."""
        captured = {}

        class _CapturingEngine:
            async def run_loop(self, loop_session):
                captured["session"] = loop_session
                return None

        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=_provider([]),
            tools=[],
            engine=_CapturingEngine(),
        )
        _run(runner.run())

        self.assertIsNone(captured["session"].inbox)
        self.assertIsNone(captured["session"].interrupt)
        self.assertFalse(captured["session"].interrupt_ends_turn)

    def test_an_interrupted_run_reports_partial_with_what_it_had(self):
        session = self._session()
        session.iteration = 3
        session.interrupt.interrupt_requested.set()

        provider = _provider(
            [LLMResponse(content="Found Apache on 10.0.0.5", model="test")]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=provider,
            tools=[],
            session=session,
        )
        report = _run(runner.run())

        self.assertEqual(report.status, "partial")
        self.assertIn("Stopped by the operator after 3 iteration(s)", report.findings)

    def test_a_report_that_beat_the_cancel_wins_over_the_stopped_stub(self):
        """The agent's own findings beat anything reconstructed from history."""
        session = self._session()

        class _CancellingTool(BaseTool):
            """Stands in for the cancel landing mid-turn, just before `report`."""

            def __init__(self) -> None:
                super().__init__("echo", "trip the interrupt")

            def get_params_class(self):
                return _EchoParams

            def approval_scope(self, params):
                return ApprovalScope.exact_only()

            def validate_params(self, toolParams):
                return None

            async def run(self, toolParams) -> ToolResult:
                session.interrupt.interrupt_requested.set()
                return ToolResult(success=True, output="ok")

        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(tool_name="echo", parameters={}, call_id="t1"),
                        _report_call("completed", "Apache on 10.0.0.5"),
                    ],
                )
            ]
        )
        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=provider,
            tools=[_CancellingTool()],
            session=session,
        )
        report = _run(runner.run())

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.findings, "Apache on 10.0.0.5")


class ApprovalStrategyTest(unittest.TestCase):
    """What decides whether an agent's tool call runs."""

    def _capture_loop_session(self, **runner_kwargs):
        captured = {}

        class _CapturingEngine:
            async def run_loop(self, loop_session):
                captured["session"] = loop_session
                return None

        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=_provider([]),
            tools=[],
            engine=_CapturingEngine(),
            **runner_kwargs,
        )
        _run(runner.run())
        return captured["session"]

    def test_an_agent_auto_approves_within_its_whitelist_by_default(self):
        built = self._capture_loop_session()

        self.assertIsInstance(built.approval, WhitelistAutoApproval)

    def test_a_supplied_strategy_is_what_the_loop_uses(self):
        strategy = WhitelistAutoApproval()

        built = self._capture_loop_session(approval=strategy)

        self.assertIs(built.approval, strategy)


class ReportToolUnitTest(unittest.TestCase):
    def test_report_tool_run_returns_success_and_metadata(self):
        tool = ReportTool()
        result = _run(
            tool.execute(
                {
                    "status": "completed",
                    "findings": "found x",
                    "next_steps": "do y",
                }
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["status"], "completed")
        self.assertEqual(result.metadata["findings"], "found x")
        self.assertEqual(result.metadata["next_steps"], "do y")

    def test_report_tool_invalid_status_fails_validation(self):
        tool = ReportTool()
        result = _run(
            tool.execute(
                {
                    "status": "weird",
                    "findings": "x",
                }
            )
        )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
