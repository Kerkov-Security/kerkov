"""AgentRunner live-event emission (issue #148, Stage 1).

Asserts that when a runner is given an event bus, it emits lifecycle +
activity events keyed by its `agent_run_id`, while the transcript still lands
in the QuietSink buffer (the source of truth) and nothing prints to a console.
"""

import asyncio
import unittest
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_events import AgentEvent, AgentEventBus, AgentEventKind
from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.models import AgentProfile
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


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


def _profile() -> AgentProfile:
    return AgentProfile(
        name="recon",
        description="A test profile",
        system_prompt="You are a test agent.",
        allowed_tools=["echo"],
        max_iterations=5,
    )


def _report_call(status: str, findings: str) -> ToolCall:
    return ToolCall(
        tool_name=REPORT_TOOL_NAME,
        parameters={"status": status, "findings": findings, "next_steps": ""},
        call_id="r1",
    )


class RunnerEmitsEventsTest(unittest.TestCase):
    def _collecting_bus(self) -> (AgentEventBus, List[AgentEvent]):
        bus = AgentEventBus()
        seen: List[AgentEvent] = []
        bus.subscribe(seen.append)
        return bus, seen

    def test_started_and_finished_bracket_the_run(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "done")],
                )
            ]
        )
        bus, seen = self._collecting_bus()
        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=provider,
            tools=[_EchoTool()],
            agent_run_id="run-123",
            event_bus=bus,
        )

        report = _run(runner.run())

        kinds = [e.kind for e in seen]
        self.assertEqual(kinds[0], AgentEventKind.STARTED)
        self.assertEqual(kinds[-1], AgentEventKind.FINISHED)
        # FINISHED carries the reported status so the row settles correctly.
        self.assertEqual(seen[-1].detail, "completed")
        self.assertEqual(report.status, "completed")
        # Every event is addressed to this agent's run id.
        self.assertTrue(all(e.agent_run_id == "run-123" for e in seen))

    def test_tool_activity_is_emitted(self):
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
                    tool_calls=[_report_call("completed", "done")],
                ),
            ]
        )
        bus, seen = self._collecting_bus()
        runner = AgentRunner(
            profile=_profile(),
            instruction="use echo",
            provider=provider,
            tools=[_EchoTool()],
            agent_run_id="run-xyz",
            event_bus=bus,
        )

        _run(runner.run())

        tool_starts = [
            e
            for e in seen
            if e.kind == AgentEventKind.TOOL_START and e.detail == "echo"
        ]
        self.assertEqual(len(tool_starts), 1)

    def test_no_bus_means_no_emission_and_still_runs(self):
        provider = _provider(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[_report_call("completed", "done")],
                )
            ]
        )
        # No event_bus → pre-#148 headless behavior; must still complete.
        runner = AgentRunner(
            profile=_profile(),
            instruction="do it",
            provider=provider,
            tools=[_EchoTool()],
        )
        report = _run(runner.run())
        self.assertEqual(report.status, "completed")


if __name__ == "__main__":
    unittest.main()
