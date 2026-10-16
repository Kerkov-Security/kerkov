"""Turn-boundary report delivery — the anti-fire-and-forget mechanism.

A detached agent that finishes while the master is idle has nobody to hand its
report to. The CLI drains the supervisor at each turn boundary; these tests pin
that the hand-off happens exactly once, as a `user` message, and only for
agents that have actually finished.
"""

import asyncio
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.models import AgentRunReport
from kerkov.agents.report_format import (
    build_report_result,
    format_report_for_injection,
)
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.messages.agents_message import AgentReportWakeMessage
from kerkov.core.session.kernel import SessionKernel


def _run(coro):
    return asyncio.run(coro)


class _FakeRunner:
    def __init__(self, session: AgentSession, gate: Optional[asyncio.Event]):
        self._session = session
        self._gate = gate

    def effective_max_iterations(self) -> int:
        return 30

    async def run(self) -> AgentRunReport:
        if self._gate is not None:
            await self._gate.wait()
        return AgentRunReport(
            status="completed",
            findings="admin:hunter2 on 10.0.0.5",
            next_steps="try it on SSH",
            agent_type=self._session.profile,
            agent_run_id=self._session.agent_run_id,
            engagement_id=f"eng-{self._session.id}",
        )


def _spec(gate: Optional[asyncio.Event] = None) -> AgentSpawnSpec:
    return AgentSpawnSpec(
        profile="recon",
        task="enumerate the host",
        name="host-enum",
        build_runner=lambda session, workspace: _FakeRunner(session, gate),
    )


def _create_mock_cli() -> KerkovCLI:
    """A CLI with only what `get_response` and report delivery touch."""
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.conversation_history = []
    cli.engagement_store = MagicMock()
    cli.agent_supervisor = AgentSupervisor(AgentRegistry(max_depth=2, max_total=20))
    cli.console = MagicMock()

    provider = MagicMock()
    provider.model = "test"
    cli.provder_manager = SimpleNamespace(provider=provider)
    cli.config = SimpleNamespace(
        agent_temperature=None,
        max_response_tokens=None,
        kb_clip_threshold_bytes=8000,
        tool_output_max_chars=6000,
        tool_output_head_chars=1500,
        tool_output_tail_chars=500,
    )
    cli.tools = []
    cli.tool_approval_helper = MagicMock()
    cli.approval_mode_manager = MagicMock()
    cli.output_sink = MagicMock()
    cli.token_tracker = MagicMock()
    cli.input_manager = MagicMock()
    cli.input_manager.switcher_requested = asyncio.Event()
    cli._switcher_task = None
    cli._switcher_handoff = None
    cli.compactor = None
    cli._knowledge_base = None
    cli._findings_ledger = None
    cli._transient_system_prompts = []
    cli._compaction_warning_shown = False
    cli._apply_system_prompt_if_possible = MagicMock()
    cli._ensure_compactor = AsyncMock()
    cli.engine = MagicMock()
    cli.engine.run_loop = AsyncMock(return_value=None)

    # get_response builds the turn through the kernel, which reads config,
    # provider, store and token tracker off SessionState.
    cli.state = SimpleNamespace(
        config=cli.config,
        provider_manager=cli.provder_manager,
        engagement_store=cli.engagement_store,
        token_tracker=cli.token_tracker,
    )
    cli.kernel = SessionKernel(cli.state)
    return cli


class ReportDeliveryTest(unittest.TestCase):
    def test_report_is_injected_at_the_next_turn_exactly_once(self):
        async def scenario():
            cli = _create_mock_cli()
            gate = asyncio.Event()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            sessions[0].provider = "TestProvider"
            sessions[0].model = "large-model"
            sessions[0].routing_tier = "large"
            sessions[0].routing_reason = "Task requires reasoning."
            gate.set()
            await asyncio.gather(*[s.task_handle for s in sessions])

            with patch("kerkov.cli.kerkov_cli.console"):
                await cli.get_response("what did you find?")
                await cli.get_response("anything else?")
            return cli

        cli = _run(scenario())
        reports = [
            m
            for m in cli.conversation_history
            if m.role == "user" and "admin:hunter2" in m.content
        ]
        self.assertEqual(len(reports), 1)
        self.assertIn(
            "Agent host-enum (recon, id: a1) reported [completed]",
            reports[0].content,
        )
        self.assertIn("try it on SSH", reports[0].content)
        self.assertIn("Model: TestProvider / large-model (large)", reports[0].content)
        self.assertIn("Routing: Task requires reasoning.", reports[0].content)
        # Delivered before the message the user typed that turn.
        self.assertEqual(cli.conversation_history[0].content, reports[0].content)
        self.assertEqual(cli.conversation_history[1].content, "what did you find?")

    def test_a_still_running_agent_delivers_nothing(self):
        async def scenario():
            cli = _create_mock_cli()
            gate = asyncio.Event()
            cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.sleep(0)

            with patch("kerkov.cli.kerkov_cli.console"):
                await cli.get_response("still there?")
            history = list(cli.conversation_history)

            gate.set()
            await cli.agent_supervisor.cancel_all()
            return history

        history = _run(scenario())
        self.assertEqual([m.content for m in history], ["still there?"])

    def test_a_report_already_collected_by_agent_wait_is_not_re_injected(self):
        async def scenario():
            cli = _create_mock_cli()
            gate = asyncio.Event()
            gate.set()
            cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await cli.agent_supervisor.wait()

            with patch("kerkov.cli.kerkov_cli.console"):
                await cli.get_response("next")
            return cli

        cli = _run(scenario())
        self.assertEqual([m.content for m in cli.conversation_history], ["next"])

    def test_delivered_reports_are_persisted_to_the_engagement(self):
        async def scenario():
            cli = _create_mock_cli()
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.gather(*[s.task_handle for s in sessions])

            with patch("kerkov.cli.kerkov_cli.console"):
                await cli.get_response("go on")
            return cli

        cli = _run(scenario())
        persisted = [
            call.args[0].content
            for call in cli.engagement_store.append_message.call_args_list
        ]
        self.assertTrue(any("admin:hunter2" in c for c in persisted))


class IdleWakeTest(unittest.TestCase):
    """A report landing at an idle prompt must not wait for a keystroke.

    Delivery itself still happens at a turn boundary; this is about producing
    that boundary when the master has nothing else to wait for.
    """

    def _cli_with_wake(self) -> KerkovCLI:
        cli = _create_mock_cli()
        cli.agent_focus = SimpleNamespace(is_master=True)
        cli.input_manager.wake_chat_prompt = MagicMock(return_value=True)
        cli.agent_supervisor.subscribe(cli._wake_on_finished_report)
        return cli

    def test_a_finished_report_ends_the_idle_prompt(self):
        async def scenario():
            cli = self._cli_with_wake()
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.gather(*[s.task_handle for s in sessions])
            return cli

        cli = _run(scenario())
        cli.input_manager.wake_chat_prompt.assert_called_once()

    def test_an_attached_operator_is_not_interrupted(self):
        """The prompt belongs to the agent being watched, not to the master."""

        async def scenario():
            cli = self._cli_with_wake()
            cli.agent_focus = SimpleNamespace(is_master=False)
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.gather(*[s.task_handle for s in sessions])
            return cli

        cli = _run(scenario())
        cli.input_manager.wake_chat_prompt.assert_not_called()

    def test_a_report_already_collected_does_not_wake_anything(self):
        async def scenario():
            cli = self._cli_with_wake()
            gate = asyncio.Event()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            # `agent_wait` collects it before the run reaches its finished
            # state, which is the race a detached master can actually lose.
            for session in sessions:
                session.report_delivered = True
            gate.set()
            await asyncio.gather(*[s.task_handle for s in sessions])
            return cli

        cli = _run(scenario())
        cli.input_manager.wake_chat_prompt.assert_not_called()

    def test_the_woken_turn_carries_the_report_and_no_user_message(self):
        async def scenario():
            cli = self._cli_with_wake()
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.gather(*[s.task_handle for s in sessions])

            with patch("kerkov.cli.kerkov_cli.console"):
                await AgentReportWakeMessage().execute(cli)
            return cli

        cli = _run(scenario())
        contents = [m.content for m in cli.conversation_history]
        self.assertEqual(len(contents), 1)
        self.assertIn("admin:hunter2", contents[0])
        cli.engine.run_loop.assert_awaited_once()

    def test_a_nested_child_does_not_wake_the_master(self):
        """A nested child reports to its parent, not into the master's prompt."""

        async def scenario():
            cli = self._cli_with_wake()
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=1)
            await asyncio.gather(*[s.task_handle for s in sessions])
            return cli

        cli = _run(scenario())
        cli.input_manager.wake_chat_prompt.assert_not_called()

    def test_a_woken_turn_with_nothing_to_deliver_does_not_run_the_engine(self):
        """The report was collected between the wake and the turn — no empty turn."""

        async def scenario():
            cli = self._cli_with_wake()
            gate = asyncio.Event()
            gate.set()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
            await asyncio.gather(*[s.task_handle for s in sessions])
            # A parent's `agent_wait` (or an earlier drain) took it first.
            for session in sessions:
                session.report_delivered = True

            with patch("kerkov.cli.kerkov_cli.console"):
                await AgentReportWakeMessage().execute(cli)
            return cli

        cli = _run(scenario())
        self.assertEqual(cli.conversation_history, [])
        cli.engine.run_loop.assert_not_awaited()


class ReportRoutingAttributionTest(unittest.TestCase):
    def setUp(self):
        self.session = AgentSession(
            id="a2", agent_run_id="run2", profile="analysis", task="Extract hostnames"
        )
        self.report = AgentRunReport(
            status="completed",
            findings="example.test",
            agent_type="analysis",
            agent_run_id="run2",
        )

    def test_nested_and_injected_reports_include_same_routing_attribution(self):
        self.session.provider = "TestProvider"
        self.session.model = "small-model"
        self.session.routing_tier = "small"
        self.session.routing_reason = "Explicit extraction with read-only tools."

        result = build_report_result([(self.session, self.report)])
        injected = format_report_for_injection(self.session, self.report)

        for text in (result.output, injected):
            self.assertIn("Model: TestProvider / small-model (small)", text)
            self.assertIn("Routing: Explicit extraction with read-only tools.", text)
            self.assertIn("example.test", text)
        self.assertEqual(
            result.metadata["reports"][0],
            {
                **self.report.model_dump(),
                "provider": "TestProvider",
                "model": "small-model",
                "routing_tier": "small",
                "routing_reason": "Explicit extraction with read-only tools.",
            },
        )
        self.assertNotIn("model", self.report.model_dump())

    def test_legacy_sessions_keep_existing_report_shape(self):
        result = build_report_result([(self.session, self.report)])
        injected = format_report_for_injection(self.session, self.report)

        self.assertEqual(result.metadata["reports"], [self.report.model_dump()])
        for text in (result.output, injected):
            self.assertNotIn("Model:", text)
            self.assertNotIn("Routing:", text)
            self.assertIn("example.test", text)


if __name__ == "__main__":
    unittest.main()
