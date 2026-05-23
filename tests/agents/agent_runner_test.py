"""Tests for AgentRunner — isolated agent loop + structured report."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.agents.agent_runner import AgentRunner
from src.agents.models import AgentProfile
from src.agents.report_tool import REPORT_TOOL_NAME, ReportTool
from src.cli.engagement_store import EngagementStore
from src.models.basic_models import LLMResponse, ToolCall
from src.tools.base_tool import (
    BaseTool,
    ToolApprovalLevel,
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

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.NONE

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
                    tool_calls=[_report_call("completed", "found the flag", "patch it")],
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

    def test_final_text_without_report_becomes_completed(self):
        provider = _provider(
            [LLMResponse(content="here is the answer", model="test")]
        )
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
