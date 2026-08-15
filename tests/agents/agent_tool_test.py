"""Tests for SpawnAgentTool — batch spawn, concurrency, caps."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_tool import SpawnAgentParams, SpawnAgentTool
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    kb_path_for,
)
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import (
    BaseTool,
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
            [
                _report_response("completed", "found A"),
                _report_response("completed", "found B"),
            ]
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
            [
                _report_response("completed", "found A"),
                _report_response("stuck", "blocked"),
            ]
        )
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: provider,
            registry=AgentRegistry(max_depth=2, max_total=10),
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


class _OutputTool(BaseTool):
    """Tool with distinctive output, so ingested artifacts are identifiable."""

    def __init__(self, name: str, output: str) -> None:
        super().__init__(name, "emit fixed output")
        self._output = output

    def get_params_class(self):
        return _NoopParams

    async def run(self, toolParams) -> ToolResult:
        return ToolResult(success=True, output=self._output)

    def validate_params(self, toolParams):
        return None


def _tool_then_report(tool_name: str) -> List[LLMResponse]:
    return [
        LLMResponse(
            content="",
            model="test",
            tool_calls=[
                ToolCall(tool_name=tool_name, parameters={}, call_id="t1"),
            ],
        ),
        _report_response("completed", "found a web server"),
    ]


class AgentKnowledgeBaseTest(unittest.TestCase):
    """An agent's raw output must be recallable by the master that spawned it."""

    def test_agent_output_is_written_to_the_masters_knowledge_base(self):
        provider = _provider_per_call(_tool_then_report("read"))
        with tempfile.TemporaryDirectory() as d:
            store_dir = Path(d)
            master_store = EngagementStore(store_dir=store_dir)
            master_id = master_store.start_engagement(
                mode="audit",
                model="test",
                verbosity="standard",
                approval_mode="normal",
            )
            kb = EngagementKnowledgeBase(master_id, store_dir)
            tool = SpawnAgentTool(
                agent_manager=_agent_manager(),
                master_tools=[
                    _OutputTool("read", "Apache Tomcat 9.0.30 on 10.10.10.5"),
                ],
                provider_getter=lambda: provider,
                registry=AgentRegistry(max_depth=2, max_total=10),
                master_engagement_store=master_store,
                knowledge_base_getter=lambda: kb,
            )
            params = SpawnAgentParams(
                tasks=[
                    {
                        "agent_type": "recon",
                        "instruction": "enumerate",
                        "success_criteria": "",
                    }
                ]
            )
            try:
                result = _run(tool.run(params))
                agent_id = result.metadata["reports"][0]["engagement_id"]

                hits = _run(kb.search("9.0.30"))

                self.assertTrue(result.success)
                self.assertEqual(len(hits), 1)
                # Stored under the agent's engagement id, inside the master's
                # file — which is what makes the drill-down possible.
                self.assertEqual(hits[0].engagement_id, agent_id)
                self.assertEqual(hits[0].agent_type, "recon")

                # The engagement id the master reads off the report is the one
                # that filters recall to that agent's work.
                self.assertIn(f"engagement: {agent_id}", result.output)
                scoped = _run(kb.search("9.0.30", engagement_id=agent_id))
                self.assertEqual(len(scoped), 1)

                # No private per-agent database was created.
                self.assertTrue(kb_path_for(store_dir, master_id).exists())
                self.assertFalse(kb_path_for(store_dir, agent_id).exists())
            finally:
                kb.close()

    def test_agents_run_without_a_knowledge_base_when_none_is_supplied(self):
        provider = _provider_per_call(_tool_then_report("read"))
        with tempfile.TemporaryDirectory() as d:
            store_dir = Path(d)
            master_store = EngagementStore(store_dir=store_dir)
            master_store.start_engagement(
                mode="audit",
                model="test",
                verbosity="standard",
                approval_mode="normal",
            )
            tool = SpawnAgentTool(
                agent_manager=_agent_manager(),
                master_tools=[_OutputTool("read", "output")],
                provider_getter=lambda: provider,
                registry=AgentRegistry(max_depth=2, max_total=10),
                master_engagement_store=master_store,
            )
            params = SpawnAgentParams(
                tasks=[
                    {
                        "agent_type": "recon",
                        "instruction": "enumerate",
                        "success_criteria": "",
                    }
                ]
            )
            result = _run(tool.run(params))

            self.assertTrue(result.success)
            self.assertEqual(list(store_dir.glob("*.kb.sqlite")), [])


class ProviderGetterTest(unittest.TestCase):
    def test_run_without_provider_returns_failure(self):
        tool = SpawnAgentTool(
            agent_manager=_agent_manager(),
            master_tools=[],
            provider_getter=lambda: None,
            registry=AgentRegistry(),
        )
        params = SpawnAgentParams(
            tasks=[{"agent_type": "recon", "instruction": "do", "success_criteria": ""}]
        )
        result = _run(tool.run(params))
        self.assertFalse(result.success)
        self.assertIn("No LLM provider", result.error)


if __name__ == "__main__":
    unittest.main()
