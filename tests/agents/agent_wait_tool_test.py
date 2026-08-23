"""Tests for AgentWaitTool — report collection, partials, unknown ids."""

import asyncio
import unittest
from typing import Optional

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.agent_wait_tool import AgentWaitParams, AgentWaitTool
from kerkov.agents.models import AgentRunReport


def _run(coro):
    return asyncio.run(coro)


class _FakeRunner:
    def __init__(
        self,
        session: AgentSession,
        gate: Optional[asyncio.Event],
        findings: str,
        status: str = "completed",
        next_steps: str = "",
    ):
        self._session = session
        self._gate = gate
        self._findings = findings
        self._status = status
        self._next_steps = next_steps

    def effective_max_iterations(self) -> int:
        return 30

    async def run(self) -> AgentRunReport:
        if self._gate is not None:
            await self._gate.wait()
        return AgentRunReport(
            status=self._status,
            findings=self._findings,
            next_steps=self._next_steps,
            agent_type=self._session.profile,
            agent_run_id=self._session.agent_run_id,
            engagement_id=f"eng-{self._session.id}",
        )


def _spec(
    profile: str,
    task: str,
    *,
    name: str = "",
    gate: Optional[asyncio.Event] = None,
    findings: str = "done",
    status: str = "completed",
    next_steps: str = "",
) -> AgentSpawnSpec:
    return AgentSpawnSpec(
        profile=profile,
        task=task,
        build_runner=lambda session, workspace: _FakeRunner(
            session, gate, findings, status, next_steps
        ),
        name=name,
    )


def _supervisor() -> AgentSupervisor:
    return AgentSupervisor(AgentRegistry(max_depth=2, max_total=20))


class AgentWaitTest(unittest.TestCase):
    def test_collects_reports_in_the_report_section_format(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn(
                [
                    _spec(
                        "recon",
                        "sweep",
                        name="port-sweep",
                        gate=gate,
                        findings="port 80 open",
                        next_steps="fingerprint it",
                    )
                ],
                parent_depth=0,
            )
            return await AgentWaitTool(supervisor).run(AgentWaitParams())

        result = _run(scenario())
        self.assertTrue(result.success)
        self.assertIn("### port-sweep — recon [completed] (id: a1)", result.output)
        self.assertIn("(engagement: eng-a1)", result.output)
        self.assertIn("FINDINGS:\nport 80 open", result.output)
        self.assertIn("NEXT STEPS:\nfingerprint it", result.output)
        self.assertEqual(result.metadata["agent_ids"], ["a1"])

    def test_timeout_returns_partial_with_still_running_ids(self):
        async def scenario():
            fast = asyncio.Event()
            fast.set()
            slow = asyncio.Event()
            supervisor = _supervisor()
            supervisor.spawn(
                [
                    _spec(
                        "recon",
                        "quick",
                        name="quick-look",
                        gate=fast,
                        findings="quick finding",
                    ),
                    _spec("exploit", "slow", name="slow-poke", gate=slow),
                ],
                parent_depth=0,
            )
            result = await AgentWaitTool(supervisor).run(
                AgentWaitParams(timeout_s=0.05)
            )
            slow.set()
            await supervisor.cancel_all()
            return result

        result = _run(scenario())
        # A timeout is not a failure: the finished agent's work still lands.
        self.assertTrue(result.success)
        self.assertIn("quick finding", result.output)
        self.assertIn("Still running: slow-poke (a2)", result.output)
        self.assertEqual(result.metadata["still_running"], ["a2"])

    def test_named_ids_wait_only_for_those_agents(self):
        async def scenario():
            fast = asyncio.Event()
            fast.set()
            slow = asyncio.Event()
            supervisor = _supervisor()
            supervisor.spawn(
                [
                    _spec(
                        "recon",
                        "quick",
                        name="quick-look",
                        gate=fast,
                        findings="quick finding",
                    ),
                    _spec("exploit", "slow", name="slow-poke", gate=slow),
                ],
                parent_depth=0,
            )
            result = await AgentWaitTool(supervisor).run(AgentWaitParams(ids=["a1"]))
            slow.set()
            await supervisor.cancel_all()
            return result

        result = _run(scenario())
        self.assertEqual(result.metadata["agent_ids"], ["a1"])
        self.assertEqual(result.metadata["still_running"], [])

    def test_stuck_agent_marks_the_result_failed_but_returns_the_report(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn(
                [_spec("recon", "x", gate=gate, findings="blocked", status="stuck")],
                parent_depth=0,
            )
            return await AgentWaitTool(supervisor).run(AgentWaitParams())

        result = _run(scenario())
        self.assertFalse(result.success)
        self.assertIn("blocked", result.output)
        self.assertTrue(result.metadata["any_stuck"])

    def test_waiting_with_nothing_outstanding_is_a_benign_success(self):
        result = _run(AgentWaitTool(_supervisor()).run(AgentWaitParams()))
        self.assertTrue(result.success)
        self.assertIn("No agents to wait for", result.output)

    def test_unknown_id_is_reported_as_an_error_not_a_crash(self):
        result = _run(AgentWaitTool(_supervisor()).run(AgentWaitParams(ids=["a7"])))
        self.assertFalse(result.success)
        self.assertIn("a7", result.error)


if __name__ == "__main__":
    unittest.main()
