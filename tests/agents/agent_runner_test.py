"""Tests for AgentRunner — isolated agent loop + structured report."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.models import AgentProfile
from kerkov.agents.report_tool import REPORT_TOOL_NAME, ReportTool
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.run_workspace import get_current_workspace
from kerkov.models.basic_models import LLMResponse, ToolCall
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
