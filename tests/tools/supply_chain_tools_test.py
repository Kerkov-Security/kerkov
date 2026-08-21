"""Tests for Syft SBOM generation and Grype vulnerability scanning tools."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence
from unittest.mock import patch

from kerkov.core.anchore_runtime import (
    AnchoreRuntimeError,
    AnchoreRuntimeInfo,
)
from kerkov.tools.sbom_tool import SbomGenerateTool
from kerkov.tools.supply_chain_runner import (
    AnchoreCommandResult,
    SubprocessAnchoreCommandRunner,
)
from kerkov.tools.vulnerability_scan_tool import VulnerabilityScanTool


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class _FakeRuntime:
    def __init__(self, tool: str, error: Optional[str] = None) -> None:
        self.tool = tool
        self.error = error
        self.calls: List[bool] = []

    async def resolve(self, *, install_if_missing: bool) -> AnchoreRuntimeInfo:
        self.calls.append(install_if_missing)
        if self.error:
            raise AnchoreRuntimeError(self.error)
        return AnchoreRuntimeInfo(
            binary=f"/fake/{self.tool}",
            source="fake",
            version="v1.2.3",
        )


class _FakeRunner:
    def __init__(
        self,
        report: Optional[dict[str, Any]] = None,
        result: Optional[AnchoreCommandResult] = None,
        log_text: str = "",
    ) -> None:
        self.report = report
        self.result = result or AnchoreCommandResult(exit_code=0)
        self.log_text = log_text
        self.calls: List[dict[str, Any]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
        environment: Mapping[str, str],
        display_name: str,
    ) -> AnchoreCommandResult:
        self.calls.append(
            {
                "args": list(args),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "log_path": log_path,
                "environment": dict(environment),
                "display_name": display_name,
            }
        )
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(self.log_text)
        if self.report is not None:
            if "--file" in args:
                report_path = Path(args[args.index("--file") + 1])
            else:
                output = args[args.index("--output") + 1]
                report_path = Path(output.split("=", 1)[1])
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
        return self.result


class _SupplyChainToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.project = self.workspace / "project"
        self.project.mkdir()
        (self.project / "requirements.txt").write_text(
            "requests==2.34.2\n", encoding="utf-8"
        )

    def _execute(self, tool: Any, params: dict[str, Any]) -> Any:
        module = tool.__class__.__module__
        with patch(f"{module}.get_current_workspace", return_value=self.workspace):
            return _run(tool.execute(params, call_id="call/supply-chain"))


class SbomGenerateToolTest(_SupplyChainToolTestBase):
    def test_execution_gate_prevents_runtime_and_artifact_side_effects(self) -> None:
        runtime = _FakeRuntime("syft")
        runner = _FakeRunner()
        tool = SbomGenerateTool(runner=runner, runtime=runtime)

        result = self._execute(tool, {"target": "project"})

        self.assertFalse(result.success)
        self.assertEqual(result.metadata["policy"], "execution_disabled")
        self.assertEqual(runtime.calls, [])
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_generates_bounded_syft_json_summary_and_artifact(self) -> None:
        artifacts = [
            {
                "name": f"package-{index:03d}",
                "version": str(index),
                "type": "python",
                "purl": f"pkg:pypi/package-{index}@{index}",
                "licenses": [{"value": "MIT"}],
                "locations": [{"path": f"requirements-{index}.txt"}],
            }
            for index in range(80)
        ]
        runner = _FakeRunner(
            {"descriptor": {"version": "1.45.1"}, "artifacts": artifacts}
        )
        runtime = _FakeRuntime("syft")
        tool = SbomGenerateTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {
                "target": "project",
                "source_type": "directory",
                "max_packages": 5,
                "exclude": ["**/node_modules/**"],
            },
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["package_count"], 80)
        self.assertEqual(payload["returned_package_count"], 5)
        self.assertTrue(payload["packages_truncated"])
        self.assertEqual(payload["source"], "project")
        self.assertTrue(Path(payload["artifacts"]["report"]).is_file())
        args = runner.calls[0]["args"]
        self.assertEqual(args[0], "/fake/syft")
        self.assertIn("--from", args)
        self.assertEqual(args[args.index("--from") + 1], "dir")
        self.assertIn("**/node_modules/**", args)
        self.assertEqual(runtime.calls, [True])

    def test_routes_registry_image_without_treating_it_as_local_path(self) -> None:
        runner = _FakeRunner({"descriptor": {}, "artifacts": []})
        tool = SbomGenerateTool(
            runner=runner,
            runtime=_FakeRuntime("syft"),
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {
                "target": "alpine:3.22",
                "source_type": "image",
                "image_source": "registry",
                "scope": "all-layers",
                "output_format": "cyclonedx-json",
            },
        )

        self.assertTrue(result.success)
        args = runner.calls[0]["args"]
        self.assertEqual(args[1], "alpine:3.22")
        self.assertEqual(args[args.index("--from") + 1], "registry")
        self.assertEqual(args[args.index("--scope") + 1], "all-layers")
        self.assertIn("cyclonedx-json=", args[args.index("--output") + 1])

    def test_rejects_path_escape_and_image_urls_before_runtime(self) -> None:
        runtime = _FakeRuntime("syft")
        tool = SbomGenerateTool(
            runner=_FakeRunner(),
            runtime=runtime,
            execution_enabled=True,
        )

        escaped = self._execute(tool, {"target": "../outside"})
        image_url = self._execute(
            tool,
            {"target": "https://registry.test/image", "source_type": "image"},
        )
        embedded_credentials = self._execute(
            tool,
            {"target": "user:password@registry.test/image", "source_type": "image"},
        )

        self.assertFalse(escaped.success)
        self.assertIn("outside", escaped.error)
        self.assertFalse(image_url.success)
        self.assertIn("not a URL", image_url.error)
        self.assertFalse(embedded_credentials.success)
        self.assertIn("credentials", embedded_credentials.error)
        self.assertEqual(runtime.calls, [])

    def test_runtime_failure_is_bounded_and_keeps_diagnostic_artifacts(self) -> None:
        tool = SbomGenerateTool(
            runner=_FakeRunner(),
            runtime=_FakeRuntime("syft", error="download failed"),
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertFalse(result.success)
        payload = json.loads(result.output)
        self.assertIn("download failed", result.error)
        self.assertTrue(Path(payload["artifacts"]["log"]).is_file())


class VulnerabilityScanToolTest(_SupplyChainToolTestBase):
    @staticmethod
    def _match(
        vuln_id: str,
        severity: str,
        package: str,
        *,
        fix_state: str = "not-fixed",
    ) -> dict[str, Any]:
        return {
            "vulnerability": {
                "id": vuln_id,
                "severity": severity,
                "description": "description " * 200,
                "dataSource": "https://example.test/advisory",
                "urls": ["https://example.test/CVE"],
                "fix": {"state": fix_state, "versions": ["9.9.9"]},
                "risk": 82.5,
                "epss": [{"cve": vuln_id, "epss": 0.8, "percentile": 0.99}],
            },
            "artifact": {
                "name": package,
                "version": "1.0.0",
                "type": "python",
                "purl": f"pkg:pypi/{package}@1.0.0",
                "locations": [{"path": "requirements.txt"}],
            },
        }

    def test_scans_existing_sbom_with_cached_database_and_filters_summary(self) -> None:
        sbom = self.workspace / "artifacts" / "sbom" / "input.syft.json"
        sbom.parent.mkdir(parents=True)
        sbom.write_text("{}", encoding="utf-8")
        report = {
            "descriptor": {
                "version": "0.99.1",
                "db": {
                    "built": "2026-08-21T00:00:00Z",
                    "schemaVersion": 6,
                    "checksum": "sha256:abc",
                    "location": "/secret/cache/path",
                },
            },
            "matches": [
                self._match("CVE-LOW", "Low", "low-package"),
                self._match("CVE-CRIT", "Critical", "critical-package"),
                self._match("CVE-HIGH", "High", "high-package"),
            ],
        }
        runner = _FakeRunner(report)
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {
                "target": "artifacts/sbom/input.syft.json",
                "source_type": "sbom",
                "minimum_severity": "high",
                "max_findings": 1,
                "update_database": False,
                "only_fixed": True,
            },
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["match_count"], 3)
        self.assertEqual(payload["filtered_match_count"], 2)
        self.assertEqual(payload["findings"][0]["id"], "CVE-CRIT")
        self.assertTrue(payload["findings_truncated"])
        self.assertNotIn("location", payload["database"])
        self.assertLessEqual(len(payload["findings"][0]["description"]), 1200)
        call = runner.calls[0]
        self.assertEqual(call["args"][call["args"].index("--from") + 1], "sbom")
        self.assertIn("--only-fixed", call["args"])
        self.assertEqual(call["environment"]["GRYPE_DB_AUTO_UPDATE"], "false")

    def test_findings_do_not_turn_a_successful_scan_into_tool_failure(self) -> None:
        runner = _FakeRunner(
            {"descriptor": {}, "matches": [self._match("CVE-1", "High", "pkg")]}
        )
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertTrue(result.success)
        self.assertEqual(json.loads(result.output)["match_count"], 1)

    def test_failed_scan_returns_only_bounded_log_tail(self) -> None:
        noise = "beginning-marker\n" + ("x" * 20_000) + "\nending-marker\n"
        runner = _FakeRunner(
            result=AnchoreCommandResult(exit_code=1),
            log_text=noise,
        )
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertFalse(result.success)
        payload = json.loads(result.output)
        self.assertNotIn("beginning-marker", payload["diagnostic_tail"])
        self.assertIn("ending-marker", payload["diagnostic_tail"])
        self.assertLess(len(result.output), 6000)

    def test_requires_active_workspace(self) -> None:
        tool = VulnerabilityScanTool(execution_enabled=True, binary="grype")

        with patch(
            "kerkov.tools.vulnerability_scan_tool.get_current_workspace",
            return_value=None,
        ):
            result = _run(tool.execute({"target": "."}))

        self.assertFalse(result.success)
        self.assertEqual(result.metadata["policy"], "workspace_required")


class SubprocessAnchoreCommandRunnerTest(unittest.TestCase):
    def test_persists_output_without_returning_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "scanner.log"
            runner = SubprocessAnchoreCommandRunner()

            result = _run(
                runner.run(
                    [sys.executable, "-c", "print('scanner output')"],
                    cwd=root,
                    timeout_seconds=5,
                    log_path=log_path,
                    environment={"TOOL_TEST_VALUE": "safe"},
                    display_name="test scanner",
                )
            )

            self.assertTrue(result.success)
            self.assertIn("scanner output", log_path.read_text(encoding="utf-8"))

    def test_timeout_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = SubprocessAnchoreCommandRunner()

            result = _run(
                runner.run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    cwd=root,
                    timeout_seconds=0.05,
                    log_path=root / "timeout.log",
                    environment={},
                    display_name="test scanner",
                )
            )

            self.assertFalse(result.success)
            self.assertTrue(result.timed_out)
            self.assertIn("timed out", result.error or "")


if __name__ == "__main__":
    unittest.main()
