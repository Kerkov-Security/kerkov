"""Tests for compact Docker Compose lifecycle monitoring."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Optional, Sequence
from unittest.mock import AsyncMock, patch

from kerkov.tools.container_lifecycle_tool import (
    ContainerAction,
    ContainerLifecycleTool,
    DockerCommandResult,
    SubprocessDockerCommandRunner,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _compose_record(
    *,
    service: str = "web",
    state: str = "running",
    health: str = "",
    published_port: int = 8080,
) -> dict[str, Any]:
    return {
        "Name": f"lab-{service}-1",
        "Service": service,
        "State": state,
        "Health": health,
        "ExitCode": 0,
        "Publishers": [
            {
                "URL": "0.0.0.0",
                "TargetPort": 80,
                "PublishedPort": published_port,
                "Protocol": "tcp",
            }
        ],
    }


class _FakeDockerRunner:
    def __init__(
        self,
        *,
        snapshots: Optional[List[str]] = None,
        launch_result: Optional[DockerCommandResult] = None,
        launch_log: str = "compose startup output\n",
    ) -> None:
        self.snapshots = list(snapshots or [json.dumps([_compose_record()])])
        self.launch_result = launch_result or DockerCommandResult(exit_code=0)
        self.launch_log = launch_log
        self.calls: List[List[str]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        capture_output: bool = True,
    ) -> DockerCommandResult:
        del cwd, timeout_seconds, capture_output
        command = list(args)
        self.calls.append(command)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if "config" in command:
            result = DockerCommandResult(exit_code=0, stdout="web\n")
        elif "up" in command:
            result = self.launch_result
        elif "ps" in command:
            snapshot = (
                self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            )
            result = DockerCommandResult(exit_code=0, stdout=snapshot)
        elif "logs" in command:
            result = DockerCommandResult(exit_code=0, stdout="diagnostic log\n")
        else:
            result = DockerCommandResult(exit_code=0)

        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("$ " + " ".join(command) + "\n")
            if "up" in command:
                log_file.write(self.launch_log)
            log_file.write(result.stdout)
            log_file.write(result.stderr)
        return result


class _ContainerLifecycleToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.compose_dir = self.root / "lab"
        self.compose_dir.mkdir()
        (self.compose_dir / "compose.yaml").write_text(
            "services:\n  web:\n    image: example/web\n", encoding="utf-8"
        )
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _execute(
        self,
        runner: _FakeDockerRunner,
        params: dict[str, Any],
        *,
        execution_enabled: bool = True,
    ) -> Any:
        tool = ContainerLifecycleTool(
            runner=runner,
            execution_enabled=execution_enabled,
        )
        with patch(
            "kerkov.tools.container_lifecycle_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            return _run(tool.execute(params, call_id="call/container"))


class ContainerLifecycleExecutionPolicyTest(_ContainerLifecycleToolTestBase):
    def test_disabled_by_default_before_runner_or_log_side_effects(self) -> None:
        for action in ContainerAction:
            with self.subTest(action=action.value):
                runner = _FakeDockerRunner()
                tool = ContainerLifecycleTool(runner=runner)
                with patch(
                    "kerkov.tools.container_lifecycle_tool.get_current_workspace",
                    return_value=self.workspace,
                ):
                    result = _run(
                        tool.execute(
                            {
                                "action": action.value,
                                "compose_dir": str(self.compose_dir),
                            },
                            call_id="call/container",
                        )
                    )

                self.assertFalse(result.success)
                self.assertEqual(
                    result.metadata,
                    {
                        "policy": "execution_disabled",
                        "action": action.value,
                    },
                )
                self.assertIn("EXECUTION_ENABLED=true", result.error)
                self.assertEqual(runner.calls, [])
                self.assertFalse((self.workspace / "artifacts").exists())


class SubprocessRunnerTest(unittest.TestCase):
    def test_captured_command_is_returned_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "command.log"
            runner = SubprocessDockerCommandRunner()

            result = _run(
                runner.run(
                    [sys.executable, "-c", "print('compact output')"],
                    cwd=root,
                    timeout_seconds=5,
                    log_path=log_path,
                )
            )

            self.assertTrue(result.success)
            self.assertEqual(result.stdout.strip(), "compact output")
            self.assertIn("compact output", log_path.read_text(encoding="utf-8"))

    def test_streamed_command_writes_log_but_returns_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "command.log"
            runner = SubprocessDockerCommandRunner()

            result = _run(
                runner.run(
                    [sys.executable, "-c", "print('x' * 50000)"],
                    cwd=root,
                    timeout_seconds=5,
                    log_path=log_path,
                    capture_output=False,
                )
            )

            self.assertTrue(result.success)
            self.assertEqual(result.stdout, "")
            self.assertGreater(log_path.stat().st_size, 50_000)

    def test_timeout_kills_command_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "command.log"
            runner = SubprocessDockerCommandRunner()

            result = _run(
                runner.run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    cwd=root,
                    timeout_seconds=0.05,
                    log_path=log_path,
                )
            )

            self.assertFalse(result.success)
            self.assertTrue(result.timed_out)
            self.assertIn("timed out", result.error or "")


class StartWaitTest(_ContainerLifecycleToolTestBase):
    def test_waits_locally_until_healthcheck_is_ready(self) -> None:
        runner = _FakeDockerRunner(
            snapshots=[
                json.dumps([_compose_record(health="starting")]),
                json.dumps([_compose_record(health="healthy")]),
            ]
        )

        result = self._execute(
            runner,
            {
                "action": "start_wait",
                "compose_dir": str(self.compose_dir),
                "timeout_seconds": 5,
                "poll_interval_seconds": 0.25,
            },
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertTrue(result.metadata["ready"])
        self.assertIn("healthchecks healthy", payload["readiness_basis"])
        self.assertEqual(payload["containers"][0]["service"], "web")
        self.assertEqual(payload["containers"][0]["ports"], ["0.0.0.0:8080->80/tcp"])
        self.assertTrue(Path(result.metadata["log_artifact"]).is_file())
        self.assertEqual(sum("ps" in call for call in runner.calls), 2)

    def test_noisy_startup_output_stays_out_of_success_result(self) -> None:
        noise = "docker progress redraw\r" * 10_000
        runner = _FakeDockerRunner(
            snapshots=[json.dumps([_compose_record(health="healthy")])],
            launch_log=noise,
        )

        result = self._execute(
            runner,
            {
                "action": "start_wait",
                "compose_dir": str(self.compose_dir),
                "timeout_seconds": 5,
            },
        )

        self.assertTrue(result.success)
        self.assertLess(len(result.output), 3000)
        self.assertNotIn("docker progress redraw", result.output)
        self.assertGreater(
            Path(result.metadata["log_artifact"]).stat().st_size, 100_000
        )

    def test_failed_start_returns_only_bounded_log_tail(self) -> None:
        noise = "beginning-marker\n" + ("x" * 20_000) + "\nending-marker\n"
        runner = _FakeDockerRunner(
            launch_result=DockerCommandResult(exit_code=1, error="compose failed"),
            launch_log=noise,
        )

        result = self._execute(
            runner,
            {
                "action": "start_wait",
                "compose_dir": str(self.compose_dir),
            },
        )

        self.assertFalse(result.success)
        self.assertIn("compose failed", result.error)
        self.assertLess(len(result.output), 6000)
        self.assertNotIn("beginning-marker", result.output)
        self.assertIn("ending-marker", result.output)

    def test_http_mode_requires_probe_and_reports_probe_readiness(self) -> None:
        runner = _FakeDockerRunner()
        missing_probe = self._execute(
            runner,
            {
                "action": "start_wait",
                "compose_dir": str(self.compose_dir),
                "readiness": "http",
            },
        )
        self.assertFalse(missing_probe.success)
        self.assertIn("probe_url", missing_probe.error)

        tool = ContainerLifecycleTool(runner=runner, execution_enabled=True)
        with (
            patch(
                "kerkov.tools.container_lifecycle_tool.get_current_workspace",
                return_value=self.workspace,
            ),
            patch.object(tool, "_probe_http", return_value=(True, "HTTP 200")),
        ):
            ready = _run(
                tool.execute(
                    {
                        "action": "start_wait",
                        "compose_dir": str(self.compose_dir),
                        "readiness": "http",
                        "probe_url": "http://127.0.0.1:8080/",
                    }
                )
            )

        self.assertTrue(ready.success)
        self.assertIn("HTTP probe ready", json.loads(ready.output)["readiness_basis"])

    def test_timeout_collects_diagnostic_logs_once(self) -> None:
        runner = _FakeDockerRunner(snapshots=["[]"])
        now_values = iter([0.0, 0.0, 1.1, 1.2])
        tool = ContainerLifecycleTool(
            runner=runner,
            now_fn=lambda: next(now_values),
            execution_enabled=True,
        )
        with (
            patch(
                "kerkov.tools.container_lifecycle_tool.get_current_workspace",
                return_value=self.workspace,
            ),
            patch(
                "kerkov.tools.container_lifecycle_tool.asyncio.sleep",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = _run(
                tool.execute(
                    {
                        "action": "start_wait",
                        "compose_dir": str(self.compose_dir),
                        "timeout_seconds": 1,
                    }
                )
            )

        self.assertFalse(result.success)
        self.assertIn("not ready", (result.error or "").lower())
        self.assertEqual(sum("logs" in call for call in runner.calls), 1)
        self.assertIn("diagnostic log", json.loads(result.output)["diagnostic_tail"])


class StatusAndDownTest(_ContainerLifecycleToolTestBase):
    def test_status_accepts_json_lines_from_compose(self) -> None:
        raw = "\n".join(
            [
                json.dumps(_compose_record(service="web")),
                json.dumps(_compose_record(service="db", published_port=5432)),
            ]
        )
        runner = _FakeDockerRunner(snapshots=[raw])

        result = self._execute(
            runner,
            {"action": "status", "compose_dir": str(self.compose_dir)},
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(result.metadata["status"], "ready")
        self.assertEqual(len(payload["containers"]), 2)

    def test_status_bounds_untrusted_compose_fields(self) -> None:
        oversized = _compose_record()
        oversized["Name"] = "n" * 10_000
        oversized["Service"] = "s" * 10_000
        oversized["Publishers"] = [
            {
                "URL": "u" * 1000,
                "TargetPort": 80,
                "PublishedPort": 8080,
                "Protocol": "tcp",
            }
            for _ in range(100)
        ]
        runner = _FakeDockerRunner(snapshots=[json.dumps([oversized])])

        result = self._execute(
            runner,
            {"action": "status", "compose_dir": str(self.compose_dir)},
        )

        payload = json.loads(result.output)
        container = payload["containers"][0]
        self.assertLess(len(result.output), 5000)
        self.assertLessEqual(len(container["name"]), 200)
        self.assertLessEqual(len(container["service"]), 200)
        self.assertEqual(len(container["ports"]), 10)

    def test_down_can_remove_volumes_without_returning_command_output(self) -> None:
        runner = _FakeDockerRunner(launch_log="noisy shutdown output" * 1000)

        result = self._execute(
            runner,
            {
                "action": "down",
                "compose_dir": str(self.compose_dir),
                "remove_volumes": True,
            },
        )

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["status"], "stopped")
        self.assertIn("--volumes", runner.calls[-1])
        self.assertNotIn("shutdown output", result.output)

    def test_rejects_directory_without_compose_file(self) -> None:
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        runner = _FakeDockerRunner()

        result = self._execute(
            runner,
            {"action": "status", "compose_dir": str(empty_dir)},
        )

        self.assertFalse(result.success)
        self.assertIn("No Compose file", result.error)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()


class ContainerInventoryRegistrationTest(_ContainerLifecycleToolTestBase):
    def test_tool_registers_start_and_stop_in_shared_ledger(self):
        from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
        from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
        from kerkov.core.knowledge_base.models import LedgerSection

        async def scenario():
            kb = EngagementKnowledgeBase("root", self.root)
            ledger = FindingsLedger(kb)
            try:
                runner = _FakeDockerRunner(
                    snapshots=[json.dumps([_compose_record(health="healthy")])]
                )
                tool = ContainerLifecycleTool(
                    runner=runner, execution_enabled=True, ledger_getter=lambda: ledger
                )
                params = {"action": "start_wait", "compose_dir": str(self.compose_dir)}
                result = await tool.execute(params)
                assert result.success
                assert json.loads(result.output)["inventory"]["status"] == "registered"
                assert len(await ledger.list(LedgerSection.CONTAINERS)) == 1
                assert len(await ledger.list(LedgerSection.TARGETS)) == 1
                result = await tool.execute(
                    {**params, "action": "down", "project_name": "lab"}
                )
                assert result.success
                assert (await ledger.list(LedgerSection.CONTAINERS))[0].entry[
                    "state"
                ] == "stopped"
            finally:
                kb.close()

        _run(scenario())

    def test_failed_snapshot_reads_do_not_remove_existing_container(self):
        from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
        from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
        from kerkov.core.knowledge_base.models import LedgerSection

        async def scenario():
            kb = EngagementKnowledgeBase("root", self.root)
            ledger = FindingsLedger(kb)
            try:
                tool = ContainerLifecycleTool(
                    runner=_FakeDockerRunner(),
                    execution_enabled=True,
                    ledger_getter=lambda: ledger,
                )
                params = {"action": "status", "compose_dir": str(self.compose_dir)}
                assert (await tool.execute(params)).success
                tool._runner = _FakeDockerRunner(snapshots=["invalid JSON"])
                clock = iter([0.0, 0.0, 1.1, 1.2])
                tool._now = lambda: next(clock)
                with patch(
                    "kerkov.tools.container_lifecycle_tool.asyncio.sleep",
                    new=AsyncMock(return_value=None),
                ):
                    result = await tool.execute(
                        {**params, "action": "start_wait", "timeout_seconds": 1}
                    )
                assert not result.success
                assert json.loads(result.output)["snapshot_read"] is False
                row = (await ledger.list(LedgerSection.CONTAINERS))[0]
                assert row.entry["state"] == "running" and row.entry["ready"]
            finally:
                kb.close()

        _run(scenario())

    def test_unavailable_or_failed_registration_does_not_claim_docker_failed(self):
        for getter, status in [
            (lambda: None, "unavailable"),
            (lambda: object(), "failed"),
        ]:
            tool = ContainerLifecycleTool(
                runner=_FakeDockerRunner(), execution_enabled=True, ledger_getter=getter
            )
            result = _run(
                tool.execute({"action": "status", "compose_dir": str(self.compose_dir)})
            )
            assert result.success
            assert json.loads(result.output)["inventory"]["status"] == status


def test_registration_stays_with_originating_engagement_during_context_switch(tmp_path):
    from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
    from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
    from kerkov.core.knowledge_base.models import LedgerSection

    async def scenario():
        original_kb = EngagementKnowledgeBase("original", tmp_path)
        next_kb = EngagementKnowledgeBase("next", tmp_path)
        original, following = FindingsLedger(original_kb), FindingsLedger(next_kb)
        current = original

        class SwitchingRunner(_FakeDockerRunner):
            async def run(self, *args, **kwargs):
                nonlocal current
                current = following
                return await super().run(*args, **kwargs)

        compose = tmp_path / "compose"
        compose.mkdir()
        (compose / "compose.yaml").write_text("services: {}")
        tool = ContainerLifecycleTool(
            runner=SwitchingRunner(),
            execution_enabled=True,
            ledger_getter=lambda: current,
        )
        try:
            result = await tool.execute(
                {"action": "status", "compose_dir": str(compose)}
            )
            assert result.success
            assert len(await original.list(LedgerSection.CONTAINERS)) == 1
            assert await following.list() == []
        finally:
            original_kb.close()
            next_kb.close()

    _run(scenario())
