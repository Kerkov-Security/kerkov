"""Tests for SpawnAgentTool — validation, caps, and the detached spawn contract."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import (
    SPAWN_TOOL_NAME,
    AgentTaskSpec,
    SpawnAgentParams,
    SpawnAgentTool,
)
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    kb_path_for,
)
from kerkov.core.knowledge_base.models import ArtifactKind
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import (
    BaseTool,
    ToolParams,
    ToolResult,
)
from kerkov.tools.recall_tool import RecallTool


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


def _make_tool(
    *,
    master_tools: Optional[List[BaseTool]] = None,
    provider=None,
    registry: Optional[AgentRegistry] = None,
    parent_depth: int = 0,
    detached: bool = True,
    max_batch: int = 5,
    max_iterations: Optional[int] = None,
    master_engagement_store: Optional[EngagementStore] = None,
    knowledge_base_getter=None,
    max_concurrent: int = 5,
    supervisor: Optional[AgentSupervisor] = None,
) -> Tuple[SpawnAgentTool, AgentSupervisor]:
    registry = registry or AgentRegistry(max_depth=2, max_total=10)
    supervisor = supervisor or AgentSupervisor(
        registry,
        engagement_store=master_engagement_store,
        max_concurrent=max_concurrent,
    )
    tool = SpawnAgentTool(
        agent_manager=_agent_manager(),
        master_tools=master_tools if master_tools is not None else [],
        provider_getter=lambda: provider,
        registry=registry,
        supervisor=supervisor,
        parent_depth=parent_depth,
        detached=detached,
        max_batch=max_batch,
        max_iterations=max_iterations,
        master_engagement_store=master_engagement_store,
        knowledge_base_getter=knowledge_base_getter,
    )
    return tool, supervisor


def _params(*instructions: str, agent_type: str = "recon") -> SpawnAgentParams:
    return SpawnAgentParams(
        tasks=[
            {"agent_type": agent_type, "instruction": i, "success_criteria": ""}
            for i in instructions
        ]
    )


async def _spawn_and_collect(
    tool: SpawnAgentTool,
    supervisor: AgentSupervisor,
    params: SpawnAgentParams,
    *,
    call_id: Optional[str] = None,
) -> Tuple[ToolResult, ToolResult]:
    """Run the detached spawn, then collect every report the way `agent_wait` does."""
    from kerkov.agents.report_format import build_report_result

    if call_id is None:
        spawn_result = await tool.run(params)
    else:
        spawn_result = await tool.execute(params.model_dump(), call_id=call_id)
    outcome = await supervisor.wait()
    return spawn_result, build_report_result(
        outcome.reports, still_running=outcome.pending, unknown=outcome.unknown
    )


class ValidationTest(unittest.TestCase):
    def test_unknown_profile_rejected(self):
        tool, _ = _make_tool(provider=MagicMock())
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
        tool, _ = _make_tool(provider=MagicMock(), max_batch=2)
        err = tool.validate_params(_params("task 0", "task 1", "task 2"))
        self.assertIsNotNone(err)
        self.assertIn("Batch size", err)

    def test_unpermitted_requested_tool_rejected(self):
        tool, _ = _make_tool(provider=MagicMock())
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
        tool, _ = _make_tool(provider=MagicMock())
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
        tool, _ = _make_tool(
            provider=MagicMock(),
            registry=AgentRegistry(max_depth=1, max_total=10),
            parent_depth=1,  # at the cap already
        )
        result = _run(tool.run(_params("do")))
        self.assertFalse(result.success)
        self.assertIn("Depth cap", result.error)

    def test_total_cap_returns_failure(self):
        registry = AgentRegistry(max_depth=2, max_total=2)
        registry.record(2)
        tool, _ = _make_tool(provider=MagicMock(), registry=registry)
        result = _run(tool.run(_params("do")))
        self.assertFalse(result.success)
        self.assertIn("Total agent budget", result.error)

    def test_concurrency_cap_returns_failure(self):
        provider = _provider_per_call([_report_response()] * 4)
        tool, supervisor = _make_tool(provider=provider, max_concurrent=2)

        async def scenario():
            first = await tool.run(_params("a", "b"))
            second = await tool.run(_params("c"))
            await supervisor.wait()
            return first, second

        first, second = _run(scenario())
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertIn("AGENT_MAX_CONCURRENT", second.error)


class DetachedSpawnTest(unittest.TestCase):
    def test_spawn_returns_ids_and_next_step_without_waiting(self):
        never_finishes = asyncio.Event()

        async def blocked_complete(**kwargs):
            await never_finishes.wait()
            return _report_response()

        provider = MagicMock()
        provider.model = "test"
        provider.complete = blocked_complete
        provider.get_max_output_tokens = AsyncMock(return_value=2048)

        tool, supervisor = _make_tool(provider=provider)

        async def scenario():
            result = await tool.run(_params("sweep 10.0.0.5", "parse output"))
            active = [s.id for s in supervisor.active()]
            never_finishes.set()
            await supervisor.cancel_all()
            return result, active

        result, active = _run(scenario())

        self.assertTrue(result.success)
        self.assertEqual(active, ["a1", "a2"])
        self.assertEqual(result.metadata["agent_ids"], ["a1", "a2"])
        self.assertTrue(result.metadata["detached"])
        self.assertIn("sweep-10-0-0-5 (a1, recon): sweep 10.0.0.5", result.output)
        self.assertIn("parse-output (a2, recon): parse output", result.output)
        self.assertIn("agent_wait", result.output)
        # No findings are in the spawn result — that is the whole point.
        self.assertNotIn("FINDINGS", result.output)

    def test_batch_reports_are_collected_after_the_fact(self):
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
            tool, supervisor = _make_tool(
                master_tools=[_NoopTool("read"), _NoopTool("http")],
                provider=provider,
                master_engagement_store=master_store,
            )
            _, reports = _run(
                _spawn_and_collect(tool, supervisor, _params("task A", "task B"))
            )

        self.assertTrue(reports.success)
        self.assertIn("found A", reports.output)
        self.assertIn("found B", reports.output)
        self.assertEqual(reports.metadata["agent_ids"], ["a1", "a2"])
        self.assertEqual(len(reports.metadata["reports"]), 2)
        self.assertFalse(reports.metadata["any_stuck"])

    def test_agent_engagement_records_delegate_call_id(self):
        provider = _provider_per_call([_report_response("completed", "found A")])
        with tempfile.TemporaryDirectory() as d:
            master_store = EngagementStore(store_dir=Path(d))
            master_store.start_engagement(
                mode="audit", model="test", verbosity="standard", approval_mode="normal"
            )
            tool, supervisor = _make_tool(
                master_tools=[_NoopTool("read")],
                provider=provider,
                master_engagement_store=master_store,
            )
            _, reports = _run(
                _spawn_and_collect(
                    tool, supervisor, _params("task A"), call_id="delegate-call-123"
                )
            )
            engagement_id = reports.metadata["reports"][0]["engagement_id"]
            loaded = master_store.load_engagement(engagement_id)

        self.assertTrue(reports.success)
        self.assertEqual(loaded.metadata.parent_call_id, "delegate-call-123")

    def test_max_iterations_cap_passed_to_agent_runner(self):
        provider = MagicMock()
        provider.model = "test"
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test",
                tool_calls=[ToolCall(tool_name="read", parameters={}, call_id="c")],
            )
        )
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        tool, supervisor = _make_tool(
            master_tools=[_NoopTool("read")],
            provider=provider,
            max_iterations=2,
        )

        _, reports = _run(_spawn_and_collect(tool, supervisor, _params("loop")))

        self.assertFalse(reports.success)
        self.assertEqual(provider.complete.await_count, 2)

    def test_one_stuck_agent_marks_result_failure_but_returns_all(self):
        provider = _provider_per_call(
            [
                _report_response("completed", "found A"),
                _report_response("stuck", "blocked"),
            ]
        )
        tool, supervisor = _make_tool(provider=provider)
        _, reports = _run(
            _spawn_and_collect(tool, supervisor, _params("task A", "task B"))
        )

        self.assertFalse(reports.success)
        self.assertTrue(reports.metadata["any_stuck"])
        self.assertEqual(len(reports.metadata["reports"]), 2)
        self.assertIn("found A", reports.output)
        self.assertIn("blocked", reports.output)

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

        tool, supervisor = _make_tool(provider=provider)
        _, reports = _run(_spawn_and_collect(tool, supervisor, _params("boom", "ok")))

        entries = reports.metadata["reports"]
        self.assertEqual(len(entries), 2)
        statuses = {r["status"] for r in entries}
        self.assertIn("stuck", statuses)
        self.assertIn("completed", statuses)


class NestedDelegationTest(unittest.TestCase):
    """A nested `delegate` blocks — `agent_wait` is a master tool only."""

    def _nested_tool(self, master_tool: SpawnAgentTool) -> SpawnAgentTool:
        """Pull the `delegate` an agent would actually receive out of its runner."""
        session = AgentSession(id="a0", agent_run_id="run", profile="recon", task="t")
        runner = master_tool._build_runner(
            AgentTaskSpec(agent_type="recon", instruction="parent task"),
            target_depth=1,
            provider=MagicMock(),
            session=session,
            workspace=None,
        )
        nested = [t for t in runner.tools if t.name == SPAWN_TOOL_NAME]
        self.assertEqual(len(nested), 1, "agent should receive exactly one delegate")
        return nested[0]

    def test_the_delegate_an_agent_receives_is_not_detached(self):
        master_tool, _ = _make_tool(provider=MagicMock())
        nested = self._nested_tool(master_tool)

        self.assertFalse(nested._detached)
        self.assertNotIn("agent_wait", nested.description)
        self.assertIn("once they have all finished", nested.description)

    def test_an_agent_gets_its_children_findings_back_from_the_call(self):
        provider = _provider_per_call([_report_response("completed", "child found X")])
        master_tool, supervisor = _make_tool(provider=provider)
        nested = self._nested_tool(master_tool)

        result = _run(nested.run(_params("child task")))

        self.assertTrue(result.success)
        self.assertIn("child found X", result.output)
        self.assertIn("FINDINGS", result.output)
        self.assertNotIn("detached", result.metadata)
        # Nothing is left for the master's turn boundary to pick up.
        self.assertEqual(supervisor.drain_finished_reports(), [])

    def test_a_nested_collect_does_not_consume_the_masters_reports(self):
        """The parent's wait is scoped to its own ids, not everything pending."""
        provider = _provider_per_call(
            [
                _report_response("completed", "master agent finding"),
                _report_response("completed", "child finding"),
            ]
        )
        master_tool, supervisor = _make_tool(provider=provider)
        nested = self._nested_tool(master_tool)

        async def scenario():
            await master_tool.run(_params("master task"))
            nested_result = await nested.run(_params("child task"))
            return nested_result, supervisor.drain_finished_reports()

        nested_result, drained = _run(scenario())

        self.assertIn("child finding", nested_result.output)
        self.assertNotIn("master agent finding", nested_result.output)
        # The master's own agent is still its to collect.
        self.assertEqual([s.id for s, _ in drained], ["a1"])

    def test_a_cancelled_parent_takes_its_children_down_with_it(self):
        never_finishes = asyncio.Event()

        async def blocked_complete(**kwargs):
            await never_finishes.wait()
            return _report_response()

        provider = MagicMock()
        provider.model = "test"
        provider.complete = blocked_complete
        provider.get_max_output_tokens = AsyncMock(return_value=2048)

        master_tool, supervisor = _make_tool(provider=provider)
        nested = self._nested_tool(master_tool)

        async def scenario():
            parent = asyncio.create_task(nested.run(_params("child task")))
            while not supervisor.active():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            parent.cancel()
            await asyncio.gather(parent, return_exceptions=True)
            # Cancellation is requested, not awaited, by the collect path.
            await asyncio.gather(
                *(s.task_handle for s in supervisor.list()), return_exceptions=True
            )
            return supervisor.list()

        sessions = _run(scenario())

        self.assertTrue(sessions)
        for session in sessions:
            self.assertEqual(session.state, AgentState.CANCELLED)


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
            tool, supervisor = _make_tool(
                master_tools=[
                    _OutputTool("read", "Apache Tomcat 9.0.30 on 10.10.10.5"),
                ],
                provider=provider,
                master_engagement_store=master_store,
                knowledge_base_getter=lambda: kb,
            )
            try:
                _, reports = _run(
                    _spawn_and_collect(tool, supervisor, _params("enumerate"))
                )
                agent_id = reports.metadata["reports"][0]["engagement_id"]

                hits = _run(kb.search("9.0.30"))

                self.assertTrue(reports.success)
                self.assertEqual(len(hits), 1)
                # Stored under the agent's engagement id, inside the master's
                # file — which is what makes the drill-down possible.
                self.assertEqual(hits[0].engagement_id, agent_id)
                self.assertEqual(hits[0].agent_type, "recon")

                # The engagement id the master reads off the report is the one
                # that filters recall to that agent's work.
                self.assertIn(f"engagement: {agent_id}", reports.output)
                scoped = _run(kb.search("9.0.30", engagement_id=agent_id))
                self.assertEqual(len(scoped), 1)

                # No private per-agent database was created.
                self.assertTrue(kb_path_for(store_dir, master_id).exists())
                self.assertFalse(kb_path_for(store_dir, agent_id).exists())
            finally:
                kb.close()

    def test_a_spawned_agent_can_call_recall(self):
        # `recall` reaches an agent only if its profile whitelists the name;
        # otherwise the call comes back "tool not found" and the agent re-runs
        # work the store already holds.
        provider = _provider_per_call(
            [
                LLMResponse(
                    content="",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            tool_name="recall",
                            parameters={"query": "tomcat"},
                            call_id="t1",
                        )
                    ],
                ),
                _report_response("completed", "already enumerated"),
            ]
        )
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
            _run(
                kb.add_artifact(
                    engagement_id=master_id,
                    body="Apache Tomcat 9.0.30 on 10.10.10.5",
                    kind=ArtifactKind.TOOL_OUTPUT,
                    source="exec",
                    title="exec: nmap 10.10.10.5",
                )
            )
            tool, supervisor = _make_tool(
                master_tools=[RecallTool(lambda: kb)],
                provider=provider,
                master_engagement_store=master_store,
                knowledge_base_getter=lambda: kb,
            )
            try:
                _, reports = _run(
                    _spawn_and_collect(tool, supervisor, _params("check what we know"))
                )
                agent_id = reports.metadata["reports"][0]["engagement_id"]
                transcript = master_store.load_engagement(agent_id)

                self.assertTrue(reports.success)
                tool_messages = [m for m in transcript.messages if m.role == "tool"]
                self.assertTrue(
                    any("Apache Tomcat 9.0.30" in m.content for m in tool_messages),
                    f"recall did not reach the agent: {tool_messages}",
                )
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
            tool, supervisor = _make_tool(
                master_tools=[_OutputTool("read", "output")],
                provider=provider,
                master_engagement_store=master_store,
            )
            _, reports = _run(
                _spawn_and_collect(tool, supervisor, _params("enumerate"))
            )

            self.assertTrue(reports.success)
            self.assertEqual(list(store_dir.glob("*.kb.sqlite")), [])


class ProviderGetterTest(unittest.TestCase):
    def test_run_without_provider_returns_failure(self):
        tool, _ = _make_tool(provider=None)
        result = _run(tool.run(_params("do")))
        self.assertFalse(result.success)
        self.assertIn("No LLM provider", result.error)


if __name__ == "__main__":
    unittest.main()
