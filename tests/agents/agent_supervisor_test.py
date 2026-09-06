"""Tests for AgentSupervisor — detached lifecycle, caps, cancellation, delivery."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_registry import AgentRegistry, BudgetExceeded
from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.models import AgentProfile, AgentRunReport
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.run_workspace import RunWorkspaceManager
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


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


class _GracefulRunner(_FakeRunner):
    """Honours the interrupt the way `AgentRunner` does: stops and reports."""

    async def run(self) -> AgentRunReport:
        self.session.engagement_id = f"engagement-{self.session.id}"
        while not self.session.interrupt.interrupt_requested.is_set():
            await asyncio.sleep(0)
        self.cancelled = True
        return AgentRunReport(
            status="partial",
            findings="Stopped by the operator after 3 iteration(s).",
            agent_type=self.session.profile,
            agent_run_id=self.session.agent_run_id,
            engagement_id=self.session.engagement_id,
        )


def _graceful_spec(
    profile: str = "recon", task: str = "do the thing"
) -> AgentSpawnSpec:
    return AgentSpawnSpec(
        profile=profile,
        task=task,
        build_runner=lambda session, workspace: _GracefulRunner(session, workspace),
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
    # A runner that honours the interrupt settles in one event-loop cycle, so a
    # short grace exercises both cancel paths without the production 2s wait.
    kwargs.setdefault("cancel_grace_seconds", 0.05)
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

    def test_cancelling_before_the_task_starts_still_frees_its_slot(self):
        """No `await` between spawn and cancel: the coroutine never runs at all.

        `_supervise`'s own CancelledError handler cannot fire in that window, so
        without the done-callback the session would sit in QUEUED forever,
        holding a concurrency slot nothing can release.
        """

        async def scenario():
            # No grace window: the task is cancelled outright, exactly as the
            # synchronous teardown path does it.
            supervisor = _supervisor(max_concurrent=2, cancel_grace_seconds=0)
            sessions = supervisor.spawn([_spec(gate=asyncio.Event())], parent_depth=0)
            await supervisor.cancel_all()
            return supervisor, sessions[0]

        supervisor, session = _run(scenario())
        self.assertEqual(session.state, AgentState.CANCELLED)
        self.assertIsNotNone(session.report)
        self.assertIn("cancelled before it started", session.report.findings)
        self.assertEqual(supervisor.active(), [])


class GracefulCancelTest(unittest.TestCase):
    """A cancel is "stop and tell me what you have", not "vanish"."""

    def test_a_graceful_cancel_yields_a_partial_report_and_cancelled_state(self):
        async def scenario():
            supervisor = _supervisor()
            sessions = supervisor.spawn([_graceful_spec()], parent_depth=0)
            await asyncio.sleep(0)
            await supervisor.cancel("a1")
            return sessions[0]

        session = _run(scenario())
        self.assertEqual(session.state, AgentState.CANCELLED)
        self.assertEqual(session.report.status, "partial")
        self.assertIn("Stopped by the operator", session.report.findings)

    def test_a_run_that_ignores_the_interrupt_is_hard_cancelled_after_the_grace(self):
        async def scenario():
            supervisor = _supervisor(cancel_grace_seconds=0.02)
            sessions = supervisor.spawn([_spec(gate=asyncio.Event())], parent_depth=0)
            await asyncio.sleep(0)
            await supervisor.cancel("a1")
            return sessions[0]

        session = _run(scenario())
        self.assertTrue(session.task_handle.cancelled())
        self.assertEqual(session.state, AgentState.CANCELLED)
        self.assertEqual(session.report.status, "stuck")

    def test_stopping_one_agent_leaves_a_sibling_running_and_reportable(self):
        async def scenario():
            gate = asyncio.Event()
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [_graceful_spec(), _spec(gate=gate, findings="sibling finding")],
                parent_depth=0,
            )
            await asyncio.sleep(0)
            await supervisor.cancel("a1")
            still_running = sessions[1].state
            gate.set()
            outcome = await supervisor.wait()
            return sessions, still_running, outcome

        sessions, still_running, outcome = _run(scenario())
        self.assertEqual(sessions[0].report.status, "partial")
        self.assertEqual(still_running, AgentState.RUNNING)
        self.assertEqual(sessions[1].state, AgentState.DONE)
        findings = {s.id: r.findings for s, r in outcome.reports}
        self.assertEqual(findings["a2"], "sibling finding")

    def test_cancel_all_shares_one_grace_window_across_every_agent(self):
        """Quitting must not scale its wait with the number of live agents."""

        async def scenario():
            supervisor = _supervisor(cancel_grace_seconds=0.2, max_concurrent=4)
            supervisor.spawn(
                [_spec(gate=asyncio.Event()) for _ in range(4)], parent_depth=0
            )
            await asyncio.sleep(0)
            started = time.monotonic()
            cancelled = await supervisor.cancel_all()
            return cancelled, time.monotonic() - started

        cancelled, elapsed = _run(scenario())
        self.assertEqual(cancelled, ["a1", "a2", "a3", "a4"])
        self.assertLess(elapsed, 0.6)

    def test_input_the_agent_never_read_is_reported_not_silently_dropped(self):
        async def scenario():
            supervisor = _supervisor(cancel_grace_seconds=0)
            sessions = supervisor.spawn([_spec(gate=asyncio.Event())], parent_depth=0)
            sessions[0].inbox.put_nowait("pivot to 10.0.0.9")
            await asyncio.sleep(0)
            await supervisor.cancel_all()
            return sessions[0]

        session = _run(scenario())
        self.assertTrue(
            any("never read" in line for line in session.events.buffer),
            session.events.buffer,
        )


class HardCancelReleasesToolWorkTest(unittest.TestCase):
    """A hard cancel has to take the running tool down with the agent.

    Every agent completion and tool call runs inside a child task created by
    `run_cancellable`, and nothing but that helper owns it: a cancel with no
    grace window (`request_cancel`, web-session close, a cancelled parent)
    unwinds the agent while the tool is still in flight. Exercised through a
    real `AgentRunner` because a fake runner never reaches the helper.
    """

    def test_a_hard_cancel_stops_the_tool_instead_of_leaving_it_running(self):
        started = asyncio.Event()
        release = asyncio.Event()
        side_effects: List[str] = []

        class _ProbeParams(ToolParams):
            pass

        class _ProbeTool(BaseTool):
            def __init__(self) -> None:
                super().__init__("probe", "block until released, then act")

            def get_params_class(self):
                return _ProbeParams

            def validate_params(self, toolParams) -> Optional[str]:
                return None

            async def run(self, toolParams) -> ToolResult:
                started.set()
                await release.wait()
                side_effects.append("probe ran")
                return ToolResult(success=True, output="done")

        provider = MagicMock()
        provider.model = "test-model"
        provider.complete = AsyncMock(
            return_value=LLMResponse(
                content="",
                model="test",
                tool_calls=[ToolCall(tool_name="probe", parameters={}, call_id="c1")],
            )
        )
        provider.get_max_output_tokens = AsyncMock(return_value=2048)
        provider.get_max_context_tokens = AsyncMock(return_value=100_000)

        def build(session, workspace):
            return AgentRunner(
                profile=AgentProfile(
                    name="prober",
                    description="a probing profile",
                    system_prompt="You are a probe.",
                    allowed_tools=["probe"],
                    max_iterations=3,
                ),
                instruction="probe it",
                provider=provider,
                tools=[_ProbeTool()],
                session=session,
            )

        async def scenario():
            supervisor = _supervisor()
            sessions = supervisor.spawn(
                [AgentSpawnSpec(profile="prober", task="probe it", build_runner=build)],
                parent_depth=0,
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            supervisor.request_cancel("a1")
            await asyncio.gather(sessions[0].task_handle, return_exceptions=True)
            # Whatever outlived the cancel would finish here.
            release.set()
            await asyncio.sleep(0.05)
            return sessions[0]

        session = _run(scenario())
        self.assertEqual(side_effects, [])
        self.assertEqual(session.state, AgentState.CANCELLED)


class DiscardTest(unittest.TestCase):
    def test_discarded_reports_never_drain_into_the_next_conversation(self):
        async def scenario():
            supervisor = _supervisor()
            supervisor.spawn([_spec(), _spec()], parent_depth=0)
            await supervisor.cancel_all()
            discarded = supervisor.discard_pending_reports()
            return discarded, supervisor.drain_finished_reports()

        discarded, drained = _run(scenario())
        self.assertEqual(discarded, 2)
        self.assertEqual(drained, [])


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
