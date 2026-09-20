"""Routing across real delegated loops, with no network-backed providers."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import AgentTaskSpec, SpawnAgentParams, SpawnAgentTool
from kerkov.agents.models import AgentProfile
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.approval_scope import ApprovalScope
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.routing import RoutingPolicy
from kerkov.routing.measurement import summarize_engagement
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class _ReadTool(BaseTool):
    def __init__(self):
        super().__init__("read", "Read test data")

    def get_params_class(self):
        return ToolParams

    def validate_params(self, params):
        return None

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    async def run(self, params):
        return ToolResult(success=True, output="test data")


class _Provider(BaseLLMProvider):
    def __init__(self):
        super().__init__("test", "conversation-model")
        # Like a transport, this object is intentionally shared by forks. It
        # records actual requests across isolated model-selection instances.
        self.backend = SimpleNamespace(
            requests=[], forks=[], invalid=set(), handler=None
        )

    def set_model(self, model):
        if model in self.backend.invalid:
            raise ValueError(f"Unavailable model: {model}")
        super().set_model(model)

    def fork(self, model=None):
        fork = super().fork(model)
        self.backend.forks.append(fork)
        return fork

    async def complete(
        self, messages, temperature=None, max_tokens=None, tools=None, on_delta=None
    ):
        self.backend.requests.append((self, self.model, tools))
        if self.backend.handler is not None:
            await self.backend.handler(self, tools)
        return LLMResponse(
            content="",
            model=self.model,
            tool_calls=[
                ToolCall(
                    tool_name="report",
                    parameters={"status": "completed", "findings": self.model},
                    call_id="report",
                )
            ],
        )

    def validate_connection(self):
        return True

    def list_models(self):
        return ["small-model", "medium-model", "large-model", "conversation-model"]

    async def get_max_context_tokens(self):
        return 32000

    async def get_max_output_tokens(self):
        return 2048


def _routine_task():
    return AgentTaskSpec(
        agent_type="analysis",
        instruction="Extract the hostnames from the supplied CSV.",
        success_criteria="Return every hostname exactly once.",
        task_kind="extract",
        allowed_tools=["read"],
    )


def _analysis_task():
    return AgentTaskSpec(
        agent_type="analysis",
        instruction="Compare breadth-first and depth-first graph traversal.",
        success_criteria="Describe their memory tradeoffs and one use case for each.",
        task_kind="analyze",
        allowed_tools=["read"],
    )


class AgentRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = EngagementStore(store_dir=Path(self.temp.name))
        self.store.start_engagement("audit", "conversation-model", "standard", "normal")
        self.provider = _Provider()
        self.policy = RoutingPolicy("small-model", "medium-model", "large-model")
        self.manager = AgentManager(agents_dir=self.temp.name)
        self.registry = AgentRegistry(max_depth=2, max_total=10)
        self.supervisor = AgentSupervisor(self.registry, engagement_store=self.store)
        self.tool = SpawnAgentTool(
            self.manager,
            [_ReadTool()],
            lambda: self.provider,
            self.registry,
            self.supervisor,
            master_engagement_store=self.store,
            routing_getter=lambda: self.policy,
        )

    def test_mixed_batch_selects_isolated_models_and_persists_decisions(self):
        root_id = self.store.engagement_id

        async def scenario():
            result = await self.tool.run(
                SpawnAgentParams(
                    tasks=[
                        _routine_task(),
                        _analysis_task(),
                        AgentTaskSpec(
                            agent_type="analysis", instruction="Analyze the evidence."
                        ),
                    ]
                )
            )
            await self.supervisor.wait()
            return result

        result = asyncio.run(scenario())
        self.assertTrue(result.success)
        self.assertEqual(self.provider.model, "conversation-model")
        self.assertEqual(
            [r[1] for r in self.provider.backend.requests],
            ["small-model", "medium-model", "large-model"],
        )
        self.assertEqual(len({id(r[0]) for r in self.provider.backend.requests}), 3)
        self.assertTrue(
            all(r[0] is not self.provider for r in self.provider.backend.requests)
        )
        small_tools = [t.name for t in self.provider.backend.requests[0][2]]
        medium_tools = [t.name for t in self.provider.backend.requests[1][2]]
        large_tools = [t.name for t in self.provider.backend.requests[2][2]]
        self.assertEqual(small_tools, ["read", "report"])
        self.assertEqual(medium_tools, ["read", "report"])
        self.assertIn("delegate", large_tools)
        self.assertEqual(
            [a["routing_tier"] for a in result.metadata["agents"]],
            ["small", "medium", "large"],
        )
        self.assertIn("small-model", result.output)
        for session in self.supervisor.list():
            saved = self.store.load_engagement(session.engagement_id).metadata
            self.assertEqual(saved.model, session.model)
            self.assertEqual(saved.provider, "_Provider")
            self.assertEqual(saved.routing_tier, session.routing_tier)
            self.assertEqual(saved.routing_reason, session.routing_reason)
        summary = summarize_engagement(self.store.store_dir, root_id)
        agents = [row for row in summary["agents"] if row["kind"] == "agent"]
        self.assertEqual(len(agents), 3)
        self.assertTrue(all(row["outcomes"] == {"completed": 1} for row in agents))

    def test_nested_children_keep_parent_policy_after_master_switch(self):
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            nested_results = []

            async def handle(provider, tools):
                if provider.model != "large-model":
                    return
                entered.set()
                await release.wait()
                delegate = next(t for t in tools if t.name == "delegate")
                nested_results.append(
                    await delegate.run(
                        SpawnAgentParams(tasks=[_routine_task(), _analysis_task()])
                    )
                )

            self.provider.backend.handler = handle
            await self.tool.run(
                SpawnAgentParams(
                    tasks=[
                        AgentTaskSpec(agent_type="general", instruction="Parent work")
                    ]
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            self.provider.set_model("new-main-model")
            self.policy = None
            release.set()
            await self.supervisor.wait()
            return nested_results

        results = asyncio.run(scenario())
        self.assertTrue(results[0].success)
        self.assertIn("small-model", results[0].output)
        self.assertIn("medium-model", results[0].output)
        self.assertEqual(
            [r[1] for r in self.provider.backend.requests],
            ["large-model", "small-model", "medium-model"],
        )
        self.assertTrue(
            all(not session.is_active for session in self.supervisor.list())
        )
        self.assertEqual(
            [s.routing_tier for s in self.supervisor.list()],
            ["large", "small", "medium"],
        )

    def test_manual_running_agent_keeps_model_when_master_changes(self):
        self.policy = None

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def handle(provider, tools):
                entered.set()
                await release.wait()

            self.provider.backend.handler = handle
            await self.tool.run(SpawnAgentParams(tasks=[_routine_task()]))
            await asyncio.wait_for(entered.wait(), 1)
            self.provider.set_model("new-main-model")
            release.set()
            outcome = await self.supervisor.wait()
            return outcome

        outcome = asyncio.run(scenario())
        session, report = outcome.reports[0]
        self.assertEqual(report.findings, "conversation-model")
        self.assertEqual(session.routing_tier, "manual")
        self.assertEqual(session.model, "conversation-model")
        self.assertIn(
            "delegate", [t.name for t in self.provider.backend.requests[0][2]]
        )

    def test_unavailable_selected_model_fails_only_its_agent_before_execution(self):
        root_id = self.store.engagement_id
        self.provider.backend.invalid.add("small-model")

        async def scenario():
            await self.tool.run(
                SpawnAgentParams(
                    tasks=[
                        _routine_task(),
                        AgentTaskSpec(agent_type="general", instruction="Other work"),
                    ]
                )
            )
            return await self.supervisor.wait()

        outcome = asyncio.run(scenario())
        self.assertEqual(
            [report.status for _, report in outcome.reports], ["stuck", "completed"]
        )
        self.assertIn("Unavailable model", outcome.reports[0][1].findings)
        self.assertEqual(
            [r[1] for r in self.provider.backend.requests], ["large-model"]
        )
        self.assertEqual(self.supervisor.list()[0].model, "small-model")
        failed = self.supervisor.list()[0]
        saved = self.store.load_engagement(failed.engagement_id)
        self.assertEqual(saved.metadata.parent_engagement_id, root_id)
        self.assertEqual(saved.metadata.provider, "_Provider")
        self.assertEqual(saved.metadata.model, "small-model")
        self.assertEqual(saved.metadata.routing_tier, "small")
        self.assertEqual(saved.metadata.routing_reason, failed.routing_reason)
        self.assertIn("Unavailable model", saved.messages[0].content)
        self.assertEqual(failed.report.engagement_id, failed.engagement_id)
        summary = summarize_engagement(self.store.store_dir, failed.engagement_id)
        self.assertEqual(summary["totals"]["outcomes"], {"failed": 1})
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["iterations"], 0)
        self.assertEqual(summary["totals"]["estimated_cost_usd"], 0)

    def test_cancellation_before_runner_starts_persists_zero_call_outcome(self):
        root_id = self.store.engagement_id

        async def scenario():
            await self.tool.execute(
                {"tasks": [_routine_task().model_dump()]}, call_id="parent-call"
            )
            self.supervisor._cancel_grace = 0
            await self.supervisor.cancel_all()

        asyncio.run(scenario())
        session = self.supervisor.list()[0]
        self.assertIsNone(session.started_at)
        self.assertEqual(self.provider.backend.requests, [])
        saved = self.store.load_engagement(session.engagement_id)
        self.assertEqual(saved.metadata.parent_engagement_id, root_id)
        self.assertEqual(saved.metadata.parent_call_id, "parent-call")
        self.assertEqual(saved.metadata.model, "small-model")
        self.assertEqual(saved.metadata.routing_tier, "small")
        self.assertEqual(session.report.engagement_id, session.engagement_id)
        summary = summarize_engagement(self.store.store_dir, root_id)
        self.assertEqual(summary["totals"]["outcomes"], {"cancelled": 1})
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["estimated_cost_usd"], 0)

    def test_invalid_configuration_does_not_spawn_or_consume_budget(self):
        def invalid_policy():
            raise ValueError("Configure three models first")

        self.tool._routing_getter = invalid_policy
        result = asyncio.run(self.tool.run(SpawnAgentParams(tasks=[_routine_task()])))
        self.assertFalse(result.success)
        self.assertIn("Configure three models", result.error)
        self.assertEqual(self.supervisor.list(), [])
        self.assertEqual(self.registry.total_spawned, 0)
        self.assertEqual(self.provider.backend.forks, [])

    def test_shadowed_analysis_profile_is_never_small(self):
        self.manager._profiles["analysis"] = AgentProfile(
            name="analysis",
            description="Custom profile",
            system_prompt="Custom instructions",
            allowed_tools=["read"],
            source="custom/AGENT.md",
        )

        async def scenario():
            await self.tool.run(SpawnAgentParams(tasks=[_routine_task()]))
            await self.supervisor.wait()

        asyncio.run(scenario())
        self.assertEqual(self.supervisor.list()[0].routing_tier, "large")

    def test_budget_failure_does_not_construct_provider_forks(self):
        self.registry.max_total = 0
        result = asyncio.run(self.tool.run(SpawnAgentParams(tasks=[_routine_task()])))
        self.assertFalse(result.success)
        self.assertEqual(self.provider.backend.forks, [])

    def test_routed_agent_still_supports_supervisor_cancellation(self):
        async def scenario():
            entered = asyncio.Event()
            forever = asyncio.Event()

            async def handle(provider, tools):
                entered.set()
                await forever.wait()

            self.provider.backend.handler = handle
            await self.tool.run(SpawnAgentParams(tasks=[_routine_task()]))
            await asyncio.wait_for(entered.wait(), 1)
            self.supervisor._cancel_grace = 0.01
            await self.supervisor.cancel_all()

        asyncio.run(scenario())
        self.assertTrue(self.supervisor.list()[0].is_finished)
        self.assertTrue(self.supervisor.list()[0].cancel_requested)
