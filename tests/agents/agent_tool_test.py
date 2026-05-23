"""Tests for SpawnAgentTool — batch spawn, concurrency, caps."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.agents.agent_manager import AgentManager
from src.agents.agent_registry import AgentRegistry
from src.agents.agent_tool import SpawnAgentParams, SpawnAgentTool
from src.agents.report_tool import REPORT_TOOL_NAME
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


class _NoopParams(ToolParams):
    pass


class _NoopTool(BaseTool):
    def __init__(self, name: str) -> None:
        super().__init__(name, "noop")

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.NONE

    def get_params_class(self):
        return _NoopParams

    async def run(self, toolParams) -> ToolResult:
        return ToolResult(success=True, output="ok")

    def validate_params(self, toolParams):
        return None


def _report_response(status: str = "completed", findings: str = "done") -> LLMResponse:
    return LLMResponse(
        content="",
        model="test",
        tool_calls=[
            ToolCall(
                tool_name=REPORT_TOOL_NAME,
                parameters={"status": status, "findings": findings, "next_steps": ""},
                call_id="r",
            )
        ],
    )


def _provider_per_call(responses_per_call: List[LLMResponse]) -> MagicMock:
    """Provider that returns the same response sequence to every agent."""
    p = MagicMock()
    p.model = "test-model"

    # Each agent run consumes responses in order, but we want every concurrent
    # agent to land at the same `report` call quickly. Use a side_effect that
    # cycles per agent invocation.
    p.complete = AsyncMock(side_effect=responses_per_call)
    p.get_max_output_tokens = AsyncMock(return_value=2048)
    p.get_max_context_tokens = AsyncMock(return_value=100_000)
    return p


def _agent_manager() -> AgentManager:
    with tempfile.TemporaryDirectory() as d:
        return AgentManager(agents_dir=d)


class ApprovalLevelTest(unittest.TestCase):
    def test_required(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: None,
            registry=AgentRegistry(),
        )
        self.assertEqual(tool.get_approval_level(), ToolApprovalLevel.REQUIRED)


class ValidationTest(unittest.TestCase):
    def test_unknown_profile_rejected(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=AgentRegistry(),
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "nonexistent",
                    "instruction": "do",
                    "success_criteria": "",
                }
            ]
        )
        err = tool.validate_params(params)
        self.assertIsNotNone(err)
        self.assertIn("nonexistent", err)

    def test_batch_too_large_rejected(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=AgentRegistry(),
            max_batch=2,
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "recon",
                    "instruction": f"task {i}",
                    "success_criteria": "",
                }
                for i in range(3)
            ]
        )
        err = tool.validate_params(params)
        self.assertIsNotNone(err)
        self.assertIn("Batch size", err)

    def test_unpermitted_requested_tool_rejected(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=AgentRegistry(),
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "analysis",
                    "instruction": "read only",
                    "success_criteria": "",
                    "allowed_tools": ["exec"],
                }
            ]
        )
        err = tool.validate_params(params)
        self.assertIsNotNone(err)
        self.assertIn("not permitted", err)
        self.assertIn("exec", err)

    def test_empty_effective_tool_set_rejected(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=AgentRegistry(),
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "recon",
                    "instruction": "do",
                    "success_criteria": "",
                    "allowed_tools": [],
                }
            ]
        )
        err = tool.validate_params(params)
        self.assertIsNotNone(err)
        self.assertIn("no usable tools", err)


class BudgetTest(unittest.TestCase):
    def test_depth_cap_returns_failure(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=AgentRegistry(max_depth=1, max_total=10),
            parent_depth=1,  # at the cap already
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "recon",
                    "instruction": "do",
                    "success_criteria": "",
                }
            ]
        )
        result = _run(tool.run(params))
        self.assertFalse(result.success)
        self.assertIn("Depth cap", result.error)

    def test_total_cap_returns_failure(self):
        registry = AgentRegistry(max_depth=2, max_total=2)
        registry.record(2)
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: MagicMock(),
            registry=registry,
        )
        params = SpawnAgentParams(
            tasks=[
                {
                    "agent_type": "recon",
                    "instruction": "do",
                    "success_criteria": "",
                }
            ]
        )
        result = _run(tool.run(params))
        self.assertFalse(result.success)
        self.assertIn("Total agent budget", result.error)


class BatchExecutionTest(unittest.TestCase):
    def test_batch_returns_concatenated_reports(self):
        provider = _provider_per_call(
            [_report_response("completed", "found A"), _report_response("completed", "found B")]
        )
        with tempfile.TemporaryDirectory() as d:
            master_store = EngagementStore(store_dir=Path(d))
            master_store.start_engagement(
                mode="audit", model="test", verbosity="standard", approval_mode="normal"
            )
            tool = SpawnAgentTool(
                agent_manager=_agent_manager(),
                master_tools=[_NoopTool("read"), _NoopTool("http")],
                provider_getter=lambda: provider,
                registry=AgentRegistry(max_depth=2, max_total=10),
                master_engagement_store=master_store,
            )
            params = SpawnAgentParams(
                tasks=[
                    {
                        "agent_type": "recon",
                        "instruction": "task A",
                        "success_criteria": "",
                    },
                    {
                        "agent_type": "recon",
                        "instruction": "task B",
                        "success_criteria": "",
                    },
                ]
            )
            result = _run(tool.run(params))

        self.assertTrue(result.success)
        self.assertIn("found A", result.output)
        self.assertIn("found B", result.output)
        self.assertEqual(len(result.metadata["reports"]), 2)
        self.assertFalse(result.metadata["any_stuck"])

    def test_agent_engagement_records_delegate_call_id(self):
        provider = _provider_per_call([_report_response("completed", "found A")])
        with tempfile.TemporaryDirectory() as d:
            master_store = EngagementStore(store_dir=Path(d))
            master_store.start_engagement(
                mode="audit", model="test", verbosity="standard", approval_mode="normal"
            )
            tool = SpawnAgentTool(
                agent_manager=_agent_manager(),
                master_tools=[_NoopTool("read")],
                provider_getter=lambda: provider,
                registry=AgentRegistry(max_depth=2, max_total=10),
                master_engagement_store=master_store,
            )
            result = _run(
                tool.execute(
                    {
                        "tasks": [
                            {
                                "agent_type": "recon",
                                "instruction": "task A",
                                "success_criteria": "",
                            }
                        ]
                    },
                    call_id="delegate-call-123",
                )
            )
            engagement_id = result.metadata["reports"][0]["engagement_id"]
            loaded = master_store.load_engagement(engagement_id)

        self.assertTrue(result.success)
        self.assertEqual(loaded.metadata.parent_call_id, "delegate-call-123")

    def test_max_iterations_cap_passed_to_agent_runner(self):
        provider = MagicMock()
        provider.model = "test"
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name="read",
                        parameters={},
                        call_id="c",
                    )
                ],
            )
        )
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[_NoopTool("read")],
            provider_getter=lambda: provider,
            registry=AgentRegistry(max_depth=2, max_total=10),
            max_iterations=2,
        )
        params = SpawnAgentParams(
            tasks=[
                {"agent_type": "recon", "instruction": "loop", "success_criteria": ""}
            ]
        )

        result = _run(tool.run(params))

        self.assertFalse(result.success)
        self.assertEqual(provider.complete.await_count, 2)

    def test_one_stuck_agent_marks_result_failure_but_returns_all(self):
        provider = _provider_per_call(
            [_report_response("completed", "found A"), _report_response("stuck", "blocked")]
        )
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: provider,
            registry=AgentRegistry(max_depth=2, max_total=10),
        )
        params = SpawnAgentParams(
            tasks=[
                {"agent_type": "recon", "instruction": "task A", "success_criteria": ""},
                {"agent_type": "recon", "instruction": "task B", "success_criteria": ""},
            ]
        )
        result = _run(tool.run(params))

        self.assertFalse(result.success)
        self.assertTrue(result.metadata["any_stuck"])
        self.assertEqual(len(result.metadata["reports"]), 2)
        self.assertIn("found A", result.output)
        self.assertIn("blocked", result.output)

    def test_crashing_agent_becomes_stuck_report_without_aborting_siblings(self):
        provider = MagicMock()
        provider.model = "test"
        provider.get_max_output_tokens = AsyncMock(return_value=2048)

        completion_counter = {"n": 0}

        async def fake_complete(**kwargs):
            completion_counter["n"] += 1
            # First agent: raises; second agent: reports completed.
            if completion_counter["n"] == 1:
                raise RuntimeError("simulated agent crash")
            return _report_response("completed", "second agent OK")

        provider.complete = fake_complete

        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: provider,
            registry=AgentRegistry(max_depth=2, max_total=10),
        )
        params = SpawnAgentParams(
            tasks=[
                {"agent_type": "recon", "instruction": "boom", "success_criteria": ""},
                {"agent_type": "recon", "instruction": "ok", "success_criteria": ""},
            ]
        )
        result = _run(tool.run(params))

        reports = result.metadata["reports"]
        self.assertEqual(len(reports), 2)
        statuses = {r["status"] for r in reports}
        self.assertIn("stuck", statuses)
        self.assertIn("completed", statuses)


class ProviderGetterTest(unittest.TestCase):
    def test_run_without_provider_returns_failure(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: None,
            registry=AgentRegistry(),
        )
        params = SpawnAgentParams(
            tasks=[
                {"agent_type": "recon", "instruction": "do", "success_criteria": ""}
            ]
        )
        result = _run(tool.run(params))
        self.assertFalse(result.success)
        self.assertIn("No LLM provider", result.error)


if __name__ == "__main__":
    unittest.main()
