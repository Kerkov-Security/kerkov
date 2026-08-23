"""AgentSupervisor — session-scoped owner of every detached agent run.

`delegate` used to own its agents: it built them, awaited them inside one
tool call, and threw the handles away. The supervisor takes that ownership so
the master can return to the prompt while agents keep running — it holds the
`asyncio.Task`s, the `AgentSession` handles, the shared run workspace, and the
report-delivery bookkeeping that guarantees a finished report reaches the
master exactly once.

`AgentRegistry` stays the budget *policy* (depth and lifetime totals); this is
the *instance* registry. Concurrency is the one cap that needs live instances,
so it is enforced here — reusing `BudgetExceeded` so callers keep one error path.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kerkov.agents.agent_registry import AgentRegistry, BudgetExceeded
from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import (
    AgentSession,
    AgentState,
    slugify_agent_name,
    truncate_task,
)
from kerkov.agents.models import AgentRunReport
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.knowledge_base.engagement_kb import resolve_root_engagement_id
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import RunWorkspaceManager, ensure_workspace_dirs

logger = get_logger("AgentSupervisor")

RunnerFactory = Callable[[AgentSession, Optional[Path]], AgentRunner]
EventCallback = Callable[[AgentSession, str], None]


@dataclass
class AgentSpawnSpec:
    """One agent to launch: its labels plus how to build its runner.

    The caller supplies `build_runner` because resolving a profile into a tool
    set (and reconstructing the nested `delegate` at the child's depth) is the
    spawn tool's job — the supervisor only owns lifecycle.
    """

    profile: str
    task: str
    build_runner: RunnerFactory
    name: str = ""


@dataclass
class WaitOutcome:
    """What `wait()` could collect: delivered reports, plus what is left."""

    reports: List[Tuple[AgentSession, AgentRunReport]]
    pending: List[AgentSession]
    unknown: List[str]


class AgentSupervisor:
    """Owns the live agents for one CLI/web session."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        workspace_manager: Optional[RunWorkspaceManager] = None,
        engagement_store: Optional[EngagementStore] = None,
        max_concurrent: int = 5,
    ):
        self._registry = registry
        self._workspace_manager = workspace_manager
        self._engagement_store = engagement_store
        self._max_concurrent = max_concurrent
        self._sessions: Dict[str, AgentSession] = {}
        self._subscribers: List[EventCallback] = []
        self._next_id = 1
        self._workspace: Optional[Tuple[str, Path]] = None

    def spawn(
        self,
        specs: Sequence[AgentSpawnSpec],
        *,
        parent_depth: int,
    ) -> List[AgentSession]:
        """Launch every spec as a detached task; raises `BudgetExceeded` on a cap."""
        count = len(specs)
        self._registry.can_spawn(count=count, parent_depth=parent_depth)

        running = len(self.active())
        if running + count > self._max_concurrent:
            raise BudgetExceeded(
                f"Concurrent agent cap reached: {running} already running + "
                f"{count} requested > AGENT_MAX_CONCURRENT={self._max_concurrent}. "
                "Collect reports with agent_wait before delegating more."
            )

        self._registry.record(count)
        workspace = self._resolve_workspace()

        sessions: List[AgentSession] = []
        for spec in specs:
            session = AgentSession(
                id=self._allocate_id(),
                agent_run_id=uuid.uuid4().hex[:12],
                profile=spec.profile,
                task=truncate_task(spec.task),
                name=self._allocate_name(spec.name, spec.task, spec.profile),
            )
            session.on_event = self._fan_out
            self._sessions[session.id] = session
            sessions.append(session)
            try:
                runner = spec.build_runner(session, workspace)
            except Exception as e:
                # One unbuildable spec fails as its own report; the rest of the
                # batch still launches.
                logger.error(f"Could not build agent {session.id}: {e!r}")
                session.report = self._stub_report(
                    session, f"Agent setup failed: {e!r}"
                )
                self._finish(session, AgentState.FAILED)
                continue
            session.max_iterations = runner.effective_max_iterations()
            task = asyncio.create_task(
                self._supervise(session, runner),
                name=f"kerkov-agent-{session.id}",
            )
            session.task_handle = task
            task.add_done_callback(partial(self._settle_if_unfinished, session))
            self._fan_out(session, "spawned")
        return sessions

    def list(self) -> List[AgentSession]:
        return list(self._sessions.values())

    def get(self, handle: str) -> Optional[AgentSession]:
        """Resolve an agent by its id (`a1`) or by its name (`google-pinger`).

        The model is shown both and will reach for either; names are unique
        within the session, so neither is ambiguous.
        """
        session = self._sessions.get(handle)
        if session is not None:
            return session
        return next((s for s in self._sessions.values() if s.name == handle), None)

    def active(self) -> List[AgentSession]:
        return [s for s in self._sessions.values() if s.is_active]

    def finished(self) -> List[AgentSession]:
        return [s for s in self._sessions.values() if s.is_finished]

    def subscribe(self, callback: EventCallback) -> None:
        """Register a status/output listener (the attached view is the consumer)."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def wait(
        self,
        ids: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
    ) -> WaitOutcome:
        """Await agents (by id or name) and collect their reports.

        With no `ids`, waits on everything not yet delivered. Explicitly named
        agents are returned even if their report was already delivered at a turn
        boundary — the model asked for it by name.
        """
        unknown: List[str] = []
        if ids is None:
            targets = [s for s in self._sessions.values() if not s.report_delivered]
            redeliver = False
        else:
            targets = []
            for handle in ids:
                session = self.get(handle)
                if session is None:
                    unknown.append(handle)
                elif session not in targets:
                    targets.append(session)
            redeliver = True

        handles = [
            s.task_handle
            for s in targets
            if s.task_handle is not None and not s.task_handle.done()
        ]
        if handles:
            await asyncio.wait(handles, timeout=timeout)

        reports: List[Tuple[AgentSession, AgentRunReport]] = []
        pending: List[AgentSession] = []
        for session in targets:
            if session.report is None:
                pending.append(session)
                continue
            if session.report_delivered and not redeliver:
                continue
            session.report_delivered = True
            reports.append((session, session.report))
        return WaitOutcome(reports=reports, pending=pending, unknown=unknown)

    def drain_finished_reports(self) -> List[Tuple[AgentSession, AgentRunReport]]:
        """Undelivered reports from agents that have finished, marked delivered."""
        drained: List[Tuple[AgentSession, AgentRunReport]] = []
        for session in self._sessions.values():
            if session.report is None or session.report_delivered:
                continue
            if not session.is_finished:
                continue
            session.report_delivered = True
            drained.append((session, session.report))
        return drained

    def discard_pending_reports(self) -> int:
        """Mark every undelivered report delivered without handing it over.

        For a conversation switch: those reports are addressed to the
        engagement that spawned them, and delivering them into the next one
        files one engagement's findings under another.
        """
        discarded = 0
        for session in self._sessions.values():
            if session.report is None or session.report_delivered:
                continue
            session.report_delivered = True
            discarded += 1
        return discarded

    async def cancel(self, handle: str) -> bool:
        """Cancel one agent (by id or name) and wait for it to settle."""
        if not self.request_cancel(handle):
            return False
        session = self.get(handle)
        if session is not None and session.task_handle is not None:
            await asyncio.gather(session.task_handle, return_exceptions=True)
        return True

    def request_cancel(self, handle: str) -> bool:
        """Ask one agent (by id or name) to stop, without awaiting it."""
        session = self.get(handle)
        if session is None or session.task_handle is None:
            return False
        if session.task_handle.done():
            return False
        self._request_cancel(session)
        return True

    async def cancel_all(self) -> List[str]:
        """Cancel every live agent and wait for them all to settle."""
        cancelled = self.request_cancel_all()
        handles = [
            s.task_handle
            for s in self._sessions.values()
            if s.task_handle is not None and not s.task_handle.done()
        ]
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        return cancelled

    def request_cancel_all(self) -> List[str]:
        """Request cancellation without awaiting — for synchronous teardown."""
        cancelled = []
        for session in self._sessions.values():
            if session.task_handle is None or session.task_handle.done():
                continue
            self._request_cancel(session)
            cancelled.append(session.id)
        return cancelled

    def _request_cancel(self, session: AgentSession) -> None:
        # The interrupt handle (Stage 3) lets the loop stop at its next
        # checkpoint; the task cancel is the backstop for a blocked await.
        if session.interrupt is not None:
            session.interrupt.start_watching_for_interrupt()
            session.interrupt.interrupt_requested.set()
        if session.task_handle is not None:
            session.task_handle.cancel()

    async def _supervise(
        self, session: AgentSession, runner: AgentRunner
    ) -> Optional[AgentRunReport]:
        """Run one agent, recording its outcome on the session either way."""
        session.started_at = time.monotonic()
        session.set_state(AgentState.RUNNING)
        try:
            report = await runner.run()
        except asyncio.CancelledError:
            session.report = self._stub_report(
                session, "Agent was cancelled before it reported."
            )
            self._finish(session, AgentState.CANCELLED)
            raise
        except Exception as e:
            logger.error(f"Agent {session.id} ({session.profile}) raised: {e!r}")
            session.report = self._stub_report(session, f"Agent crashed: {e!r}")
            self._finish(session, AgentState.FAILED)
            return session.report

        session.report = report
        session.engagement_id = report.engagement_id or session.engagement_id
        self._finish(session, AgentState.DONE)
        return report

    def _settle_if_unfinished(
        self, session: AgentSession, task: "asyncio.Task"
    ) -> None:
        """Close out a session whose task ended without `_supervise` recording it.

        A task cancelled before its first event-loop cycle never enters the
        coroutine at all, so the in-loop `CancelledError` handler cannot run.
        Left alone the session stays QUEUED — which counts as active, so it
        holds an `AGENT_MAX_CONCURRENT` slot that nothing can ever free.
        """
        if not session.is_active:
            return
        cancelled = task.cancelled()
        if session.report is None:
            session.report = self._stub_report(
                session,
                (
                    "Agent was cancelled before it started."
                    if cancelled
                    else "Agent stopped without reporting."
                ),
            )
        self._finish(session, AgentState.CANCELLED if cancelled else AgentState.FAILED)

    def _finish(self, session: AgentSession, state: AgentState) -> None:
        session.finished_at = time.monotonic()
        session.current_tool = None
        session.set_state(state)

    def _stub_report(self, session: AgentSession, findings: str) -> AgentRunReport:
        return AgentRunReport(
            status="stuck",
            findings=findings,
            next_steps="",
            agent_type=session.profile,
            agent_run_id=session.agent_run_id,
            engagement_id=session.engagement_id,
        )

    def _allocate_id(self) -> str:
        # Monotonic for the whole session: a cancelled a2 never frees its slot,
        # so an id printed in a report still resolves later.
        agent_id = f"a{self._next_id}"
        self._next_id += 1
        return agent_id

    def _allocate_name(self, requested: str, task: str, profile: str) -> str:
        """The user-facing name: what the master asked for, made unique.

        Falls back to a slug of the instruction, then to the profile — a name
        is always present, because it is the only handle the user is shown.
        """
        base = (
            slugify_agent_name(requested)
            or slugify_agent_name(task)
            or slugify_agent_name(profile)
            or "agent"
        )
        taken = {s.name for s in self._sessions.values()}
        if base not in taken:
            return base
        suffix = 2
        while f"{base}-{suffix}" in taken:
            suffix += 1
        return f"{base}-{suffix}"

    def _fan_out(self, session: AgentSession, event: str) -> None:
        for callback in list(self._subscribers):
            try:
                callback(session, event)
            except Exception as e:
                logger.warning(f"Agent event subscriber failed: {e}")

    def _resolve_workspace(self) -> Optional[Path]:
        """The engagement tree's shared workspace — master and agents both use it.

        Keyed on the root engagement, not the agent's own id: a private
        per-agent directory is invisible to the master and to sibling agents,
        which is the whole point of handing work off.
        """
        if self._workspace_manager is None or self._engagement_store is None:
            return None
        engagement_id = self._engagement_store.engagement_id
        if engagement_id is None:
            return None
        try:
            root_id = resolve_root_engagement_id(
                engagement_id, self._engagement_store.parent_engagement_id_of
            )
            if self._workspace is not None and self._workspace[0] == root_id:
                return self._workspace[1]
            path = self._workspace_manager.path_for_engagement(root_id)
            ensure_workspace_dirs(path)
            self._workspace = (root_id, path)
            return path
        except Exception as e:
            logger.warning(f"Could not resolve shared agent workspace: {e}")
            return None
