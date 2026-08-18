import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSpawnSpec, AgentSupervisor
from kerkov.agents.models import AgentRunReport
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    get_current_workspace,
    reset_current_workspace,
    set_current_workspace,
)


class RunWorkspaceManagerTest(unittest.TestCase):
    def setUp(self):
        # Each test runs in a fresh context so ContextVar state does not leak.
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def test_activate_creates_engagement_workspace_and_sets_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(tmp)

            path = manager.activate("abc123")

            self.assertEqual(path, Path(tmp).resolve() / "abc123")
            self.assertTrue(path.exists())
            self.assertFalse((path / "browser").exists())
            self.assertEqual(get_current_workspace(), path)

    def test_path_for_engagement_rejects_traversal_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")

            with self.assertRaises(ValueError):
                manager.path_for_engagement("../escape")
            with self.assertRaises(ValueError):
                manager.path_for_engagement("legit/../../escape")

    def test_path_for_engagement_rejects_absolute_engagement_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")
            absolute = str(Path(tmp) / "elsewhere")

            with self.assertRaises(ValueError):
                manager.path_for_engagement(absolute)

    def test_set_current_workspace_overrides_active_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = RunWorkspaceManager(Path(tmp) / "root")
            parent = manager.activate("parent")
            override = Path(tmp) / "agent"

            token = set_current_workspace(override)
            try:
                self.assertEqual(get_current_workspace(), override.resolve())
            finally:
                reset_current_workspace(token)

            self.assertEqual(get_current_workspace(), parent)


class _WorkspaceWritingRunner:
    """Agent stand-in that reads and writes the workspace it was handed."""

    def __init__(self, session: AgentSession, workspace: Optional[Path]):
        self._session = session
        self.workspace = workspace

    def effective_max_iterations(self) -> int:
        return 30

    async def run(self) -> AgentRunReport:
        siblings: List[str] = []
        if self.workspace is not None:
            (self.workspace / f"{self._session.id}.txt").write_text(self._session.id)
            siblings = sorted(p.name for p in self.workspace.iterdir())
        return AgentRunReport(
            status="completed",
            findings=" ".join(siblings),
            agent_type=self._session.profile,
            agent_run_id=self._session.agent_run_id,
        )


class SharedAgentWorkspaceTest(unittest.TestCase):
    """Master and its agents must land in one root-keyed directory."""

    def setUp(self):
        self._reset_token = set_current_workspace(None)

    def tearDown(self):
        reset_current_workspace(self._reset_token)

    def _spawn_two(self, tmp: Path) -> tuple:
        store = EngagementStore(store_dir=tmp / "engagements")
        root_id = store.start_engagement(
            mode="audit", model="test", verbosity="standard", approval_mode="normal"
        )
        manager = RunWorkspaceManager(tmp / "workspaces")
        master_path = manager.activate(root_id)
        supervisor = AgentSupervisor(
            AgentRegistry(max_depth=2, max_total=10),
            workspace_manager=manager,
            engagement_store=store,
        )
        runners: List[_WorkspaceWritingRunner] = []

        def build(session, workspace):
            runner = _WorkspaceWritingRunner(session, workspace)
            runners.append(runner)
            return runner

        async def scenario():
            supervisor.spawn(
                [
                    AgentSpawnSpec(profile="recon", task="a", build_runner=build),
                    AgentSpawnSpec(profile="recon", task="b", build_runner=build),
                ],
                parent_depth=0,
            )
            return await supervisor.wait()

        outcome = asyncio.run(scenario())
        return master_path, runners, outcome

    def test_master_and_agents_share_one_root_keyed_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path, runners, _ = self._spawn_two(Path(tmp))

            self.assertEqual({r.workspace for r in runners}, {master_path})
            self.assertTrue((master_path / "a1.txt").exists())
            self.assertTrue((master_path / "a2.txt").exists())

    def test_an_artifact_written_by_one_agent_is_visible_to_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, outcome = self._spawn_two(Path(tmp))

            # The second agent listed the directory after the first wrote in it.
            listings = [report.findings for _, report in outcome.reports]
            self.assertTrue(
                any(
                    "a1.txt" in listing and "a2.txt" in listing for listing in listings
                ),
                f"agents did not see each other's files: {listings}",
            )

    def test_a_different_engagement_root_sees_none_of_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path, _, _ = self._spawn_two(Path(tmp))
            other = RunWorkspaceManager(Path(tmp) / "workspaces").path_for_engagement(
                "some-other-engagement"
            )

            self.assertNotEqual(other, master_path)
            self.assertFalse(other.exists())

    def test_a_traversing_engagement_id_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SimpleNamespace(
                engagement_id="../escape",
                parent_engagement_id_of=lambda _: None,
            )
            supervisor = AgentSupervisor(
                AgentRegistry(max_depth=2, max_total=10),
                workspace_manager=RunWorkspaceManager(Path(tmp) / "workspaces"),
                engagement_store=store,
            )
            runners: List[_WorkspaceWritingRunner] = []

            def build(session, workspace):
                runners.append(_WorkspaceWritingRunner(session, workspace))
                return runners[-1]

            async def scenario():
                supervisor.spawn(
                    [AgentSpawnSpec(profile="recon", task="a", build_runner=build)],
                    parent_depth=0,
                )
                await supervisor.wait()

            asyncio.run(scenario())

            # The guard rejects the path; the agent gets no workspace rather
            # than one outside the root.
            self.assertEqual(runners[0].workspace, None)


if __name__ == "__main__":
    unittest.main()
