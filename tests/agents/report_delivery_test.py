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
from kerkov.cli.kerkov_cli import KerkovCLI


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
    )
    cli.tools = []
    cli.tool_approval_helper = MagicMock()
    cli.approval_mode_manager = MagicMock()
    cli.output_sink = MagicMock()
    cli.token_tracker = MagicMock()
    cli.input_manager = MagicMock()
    cli.compactor = None
    cli._knowledge_base = None
    cli._findings_ledger = None
    cli._transient_system_prompts = []
    cli._compaction_warning_shown = False
    cli._apply_system_prompt_if_possible = MagicMock()
    cli._ensure_compactor = AsyncMock()
    cli.engine = MagicMock()
    cli.engine.run_loop = AsyncMock(return_value=None)
    return cli


class ReportDeliveryTest(unittest.TestCase):
    def test_report_is_injected_at_the_next_turn_exactly_once(self):
        async def scenario():
            cli = _create_mock_cli()
            gate = asyncio.Event()
            sessions = cli.agent_supervisor.spawn([_spec(gate)], parent_depth=0)
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


if __name__ == "__main__":
    unittest.main()
