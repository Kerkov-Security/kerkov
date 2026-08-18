"""Tests for AgentSupervisor — detached lifecycle, caps, cancellation, delivery."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

from kerkov.agents.agent_registry import AgentRegistry, BudgetExceeded
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.models import AgentRunReport
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.run_workspace import RunWorkspaceManager


def _run(coro):
    return asyncio.run(coro)


class _FakeRunner:
    """Stands in for `AgentRunner`: finishes when its gate is released."""

    def __init__(
        self,
        session: AgentSession,
        workspace: Optional[Path],
        *,
        gate: Optional[asyncio.Event] = None,
        findings: str = "done",
        status: str = "completed",
        raises: Optional[Exception] = None,
    ):
        self.session = session
        self.workspace = workspace
        self._gate = gate
        self._findings = findings
        self._status = status
        self._raises = raises
        self.cancelled = False

    def effective_max_iterations(self) -> int:
        return 30

    async def run(self) -> AgentRunReport:
        self.session.engagement_id = f"engagement-{self.session.id}"
        try:
            if self._gate is not None:
                await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._raises is not None:
            raise self._raises
        return AgentRunReport(
            status=self._status,
            findings=self._findings,
            agent_type=self.session.profile,
            agent_run_id=self.session.agent_run_id,
            engagement_id=self.session.engagement_id,
        )


def _spec(
    profile: str = "recon",
    task: str = "do the thing",
    name: str = "",
    **runner_kwargs,
) -> AgentSpawnSpec:
    return AgentSpawnSpec(
        profile=profile,
        task=task,
        name=name,
        build_runner=lambda session, workspace: _FakeRunner(
            session, workspace, **runner_kwargs
        ),
    )


def _supervisor(**kwargs) -> AgentSupervisor:
    registry = kwargs.pop("registry", None) or AgentRegistry(max_depth=2, max_total=20)
    return AgentSupervisor(registry, **kwargs)


class SpawnTest(unittest.TestCase):
    def test_spawn_returns_running_handles_without_awaiting_them(self):
        gate = asyncio.Event()

        async def scenario():
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [_spec(task="one", gate=gate), _spec(task="two", gate=gate)],
                parent_depth=0,
            )
            # Nothing has been awaited: no report exists yet.
            states = [s.state for s in sessions]
            reports = [s.report for s in sessions]
            gate.set()
            await supervisor.wait()
            return sessions, states, reports

        sessions, states, reports = _run(scenario())
        self.assertEqual([s.id for s in sessions], ["a1", "a2"])
        self.assertTrue(all(s == AgentState.QUEUED for s in states))
        self.assertEqual(reports, [None, None])
        self.assertTrue(all(s.state == AgentState.DONE for s in sessions))

    def test_ids_are_monotonic_and_never_reused(self):
        gate = asyncio.Event()

        async def scenario():
            supervisor = _supervisor()
            first = supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            await supervisor.cancel(first[0].id)
            second = supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            gate.set()
            await supervisor.cancel_all()
            return first[0].id, second[0].id

        first_id, second_id = _run(scenario())
        self.assertEqual(first_id, "a1")
        self.assertEqual(second_id, "a2")

    def test_runner_receives_the_shared_root_keyed_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            store = EngagementStore(store_dir=Path(d) / "engagements")
            root_id = store.start_engagement(
                mode="audit", model="test", verbosity="standard", approval_mode="normal"
            )
            workspace_root = Path(d) / "workspaces"
            runners: List[_FakeRunner] = []

            def build(session, workspace):
                runner = _FakeRunner(session, workspace)
                runners.append(runner)
                return runner

            async def scenario():
                supervisor = _supervisor(
                    workspace_manager=RunWorkspaceManager(workspace_root),
                    engagement_store=store,
                )
                supervisor.spawn(
                    [
                        AgentSpawnSpec(profile="recon", task="a", build_runner=build),
                        AgentSpawnSpec(profile="recon", task="b", build_runner=build),
                    ],
                    parent_depth=0,
                )
                await supervisor.wait()

            _run(scenario())

            expected = (workspace_root / root_id).resolve()
            self.assertEqual([r.workspace for r in runners], [expected, expected])
            self.assertTrue(expected.is_dir())


class NamingTest(unittest.TestCase):
    """The name is what the user sees, so it always exists and is unique."""

    def _spawn(self, *specs) -> list:
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            sessions = supervisor.spawn(list(specs), parent_depth=0)
            await supervisor.wait()
            return sessions

        return _run(scenario())

    def test_the_requested_name_is_used_and_slugified(self):
        sessions = self._spawn(_spec(name="Google Pinger"))
        self.assertEqual(sessions[0].display_name(), "google-pinger")
        self.assertEqual(sessions[0].id, "a1")

    def test_an_unnamed_task_falls_back_to_a_slug_of_its_instruction(self):
        sessions = self._spawn(_spec(task="enumerate services on 10.0.0.5"))
        self.assertEqual(sessions[0].display_name(), "enumerate-services-10-0-0-5")

    def test_a_task_with_no_usable_words_falls_back_to_the_profile(self):
        sessions = self._spawn(_spec(profile="recon", task="... 42 ..."))
        self.assertEqual(sessions[0].display_name(), "recon")

    def test_duplicate_names_are_suffixed_rather_than_shared(self):
        sessions = self._spawn(
            _spec(name="port-scan"), _spec(name="port-scan"), _spec(name="port-scan")
        )
        self.assertEqual(
            [s.display_name() for s in sessions],
            ["port-scan", "port-scan-2", "port-scan-3"],
        )

    def test_an_agent_resolves_by_id_or_by_name(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn([_spec(name="google-pinger", gate=gate)], parent_depth=0)
            await supervisor.wait()
            return (
                supervisor.get("a1"),
                supervisor.get("google-pinger"),
                supervisor.get("nope"),
            )

        by_id, by_name, missing = _run(scenario())
        self.assertIs(by_id, by_name)
        self.assertIsNone(missing)

    def test_wait_accepts_a_name_in_place_of_an_id(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn(
                [_spec(name="google-pinger", gate=gate, findings="8 packets")],
                parent_depth=0,
            )
            return await supervisor.wait(ids=["google-pinger"])

        outcome = _run(scenario())
        self.assertEqual(outcome.unknown, [])
        self.assertEqual(outcome.reports[0][1].findings, "8 packets")


class BudgetTest(unittest.TestCase):
    def test_depth_and_total_caps_still_apply(self):
        supervisor = _supervisor(registry=AgentRegistry(max_depth=1, max_total=1))
        with self.assertRaises(BudgetExceeded):
            supervisor.spawn([_spec()], parent_depth=1)

        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            await supervisor.wait()
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)

        with self.assertRaises(BudgetExceeded):
            _run(scenario())

    def test_concurrency_cap_counts_only_live_agents(self):
        gate = asyncio.Event()

        async def scenario():
            supervisor = _supervisor(max_concurrent=2)
            supervisor.spawn([_spec(gate=gate), _spec(gate=gate)], parent_depth=0)
            with self.assertRaises(BudgetExceeded):
                supervisor.spawn([_spec(gate=gate)], parent_depth=0)

            gate.set()
            await supervisor.wait()
            # Finished agents free their slot.
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            await supervisor.cancel_all()

        _run(scenario())


class WaitTest(unittest.TestCase):
    def test_wait_returns_every_report(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn(
                [
                    _spec(task="a", gate=gate, findings="A"),
                    _spec(task="b", gate=gate, findings="B"),
                ],
                parent_depth=0,
            )
            return await supervisor.wait()

        outcome = _run(scenario())
        self.assertEqual([s.id for s, _ in outcome.reports], ["a1", "a2"])
        self.assertEqual([r.findings for _, r in outcome.reports], ["A", "B"])
        self.assertEqual(outcome.pending, [])

    def test_wait_timeout_returns_partial_and_names_the_stragglers(self):
        async def scenario():
            done_gate = asyncio.Event()
            done_gate.set()
            slow_gate = asyncio.Event()
            supervisor = _supervisor()
            supervisor.spawn(
                [_spec(gate=done_gate, findings="quick"), _spec(gate=slow_gate)],
                parent_depth=0,
            )
            outcome = await supervisor.wait(timeout=0.05)
            slow_gate.set()
            await supervisor.cancel_all()
            return outcome

        outcome = _run(scenario())
        self.assertEqual([s.id for s, _ in outcome.reports], ["a1"])
        self.assertEqual([s.id for s in outcome.pending], ["a2"])

    def test_wait_on_unknown_id_is_reported_not_raised(self):
        outcome = _run(_supervisor().wait(ids=["a9"]))
        self.assertEqual(outcome.unknown, ["a9"])
        self.assertEqual(outcome.reports, [])

    def test_default_wait_skips_already_delivered_reports(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            first = await supervisor.wait()
            second = await supervisor.wait()
            named = await supervisor.wait(ids=["a1"])
            return first, second, named

        first, second, named = _run(scenario())
        self.assertEqual(len(first.reports), 1)
        self.assertEqual(second.reports, [])
        # Explicitly named agents are re-served: the model asked for that one.
        self.assertEqual(len(named.reports), 1)


class DrainTest(unittest.TestCase):
    def test_finished_reports_drain_exactly_once(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [_spec(findings="found it", gate=gate)], parent_depth=0
            )
            # Let the task finish without going through `wait`, which is what
            # "the agent finished while the master was busy" looks like.
            await asyncio.gather(*[s.task_handle for s in sessions])
            return (
                supervisor.drain_finished_reports(),
                supervisor.drain_finished_reports(),
            )

        first, second = _run(scenario())
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][1].findings, "found it")
        self.assertEqual(second, [])

    def test_running_agents_are_not_drained(self):
        async def scenario():
            gate = asyncio.Event()
            supervisor = _supervisor()
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            await asyncio.sleep(0)
            drained = supervisor.drain_finished_reports()
            gate.set()
            await supervisor.cancel_all()
            return drained

        self.assertEqual(_run(scenario()), [])

    def test_a_crashed_agent_still_reports_as_stuck(self):
        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.spawn(
                [_spec(gate=gate, raises=RuntimeError("boom"))], parent_depth=0
            )
            outcome = await supervisor.wait()
            return outcome, supervisor.get("a1")

        outcome, session = _run(scenario())
        self.assertEqual(session.state, AgentState.FAILED)
        self.assertEqual(outcome.reports[0][1].status, "stuck")
        self.assertIn("boom", outcome.reports[0][1].findings)


class CancelTest(unittest.TestCase):
    def test_cancelling_one_agent_leaves_its_siblings_running(self):
        async def scenario():
            gate = asyncio.Event()
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [_spec(gate=gate), _spec(gate=gate)], parent_depth=0
            )
            await asyncio.sleep(0)
            await supervisor.cancel("a1")
            still_running = sessions[1].state
            gate.set()
            outcome = await supervisor.wait()
            return sessions, still_running, outcome

        sessions, still_running, outcome = _run(scenario())
        self.assertEqual(sessions[0].state, AgentState.CANCELLED)
        self.assertEqual(still_running, AgentState.RUNNING)
        self.assertEqual(sessions[1].state, AgentState.DONE)
        self.assertIn("a2", [s.id for s, _ in outcome.reports])

    def test_cancel_all_leaves_no_pending_tasks(self):
        async def scenario():
            gate = asyncio.Event()
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [_spec(gate=gate), _spec(gate=gate)], parent_depth=0
            )
            await asyncio.sleep(0)
            cancelled = await supervisor.cancel_all()
            return cancelled, sessions

        cancelled, sessions = _run(scenario())
        self.assertEqual(cancelled, ["a1", "a2"])
        for session in sessions:
            self.assertTrue(session.task_handle.done())
            self.assertEqual(session.state, AgentState.CANCELLED)


class SubscriptionTest(unittest.TestCase):
    def test_subscribers_see_spawn_and_state_transitions(self):
        events = []

        async def scenario():
            gate = asyncio.Event()
            gate.set()
            supervisor = _supervisor()
            supervisor.subscribe(
                lambda session, event: events.append((session.id, event))
            )
            supervisor.spawn([_spec(gate=gate)], parent_depth=0)
            await supervisor.wait()

        _run(scenario())
        self.assertIn(("a1", "spawned"), events)
        self.assertIn(("a1", "state"), events)


if __name__ == "__main__":
    unittest.main()
