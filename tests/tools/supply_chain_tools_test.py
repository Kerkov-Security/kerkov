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


def _from_values(args: Sequence[str]) -> List[str]:
    return [
        args[index + 1] for index, value in enumerate(args[:-1]) if value == "--from"
    ]


class _FakeRuntime:
    def __init__(self, tool: str, error: Optional[str] = None) -> None:
        self.tool = tool
        self.error = error
        self.calls = 0

    async def resolve(self) -> AnchoreRuntimeInfo:
        self.calls += 1
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
        self.assertEqual(runtime.calls, 0)
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
        self.assertNotIn("--quiet", args)
        self.assertNotIn("SYFT_QUIET", runner.calls[0]["environment"])
        self.assertEqual(runtime.calls, 1)

    def test_unknown_configured_version_is_labeled_consistently(self) -> None:
        tool = SbomGenerateTool(
            runner=_FakeRunner({"descriptor": {}, "artifacts": []}),
            binary="/configured/syft",
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["runtime"]["version"], "unknown")
        log_path = Path(payload["artifacts"]["log"])
        self.assertIn("version=unknown", log_path.read_text(encoding="utf-8"))

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

    def test_auto_image_uses_only_explicit_image_sources(self) -> None:
        runner = _FakeRunner({"descriptor": {}, "artifacts": []})
        tool = SbomGenerateTool(
            runner=runner,
            runtime=_FakeRuntime("syft"),
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {"target": "alpine:3.22", "source_type": "image"},
        )

        self.assertTrue(result.success)
        self.assertEqual(
            _from_values(runner.calls[0]["args"]),
            ["docker", "podman", "containerd", "registry"],
        )

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
        unsafe_image_targets = [
            self._execute(
                tool,
                {"target": target, "source_type": "image"},
            )
            for target in ("dir:/etc", "file:/etc/passwd", "/etc", "C:\\Windows")
        ]

        self.assertFalse(escaped.success)
        self.assertIn("outside", escaped.error)
        self.assertFalse(image_url.success)
        self.assertIn("not a URL", image_url.error)
        self.assertFalse(embedded_credentials.success)
        self.assertIn("credentials", embedded_credentials.error)
        for unsafe_result in unsafe_image_targets:
            self.assertFalse(unsafe_result.success)
            self.assertIn("image target", unsafe_result.error)
        self.assertEqual(runtime.calls, 0)

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

    def test_filters_synthetic_components_and_normalizes_spdx_metadata(self) -> None:
        absolute_source = str((self.project / "requirements.txt").resolve())
        cases = [
            (
                "cyclonedx-json",
                {
                    "metadata": {
                        "tools": {"components": [{"name": "syft", "version": "1.51.0"}]}
                    },
                    "components": [
                        {"type": "file", "name": absolute_source},
                        {
                            "type": "library",
                            "name": "requests",
                            "version": "2.34.2",
                            "purl": "pkg:pypi/requests@2.34.2",
                        },
                    ],
                },
                "library",
            ),
            (
                "spdx-json",
                {
                    "creationInfo": {
                        "creators": [
                            "Organization: Anchore, Inc",
                            "Tool: syft-1.51.0",
                        ]
                    },
                    "packages": [
                        {
                            "name": absolute_source,
                            "primaryPackagePurpose": "FILE",
                        },
                        {
                            "name": "requests",
                            "versionInfo": "2.34.2",
                            "externalRefs": [
                                {
                                    "referenceType": "purl",
                                    "referenceLocator": "pkg:pypi/requests@2.34.2",
                                }
                            ],
                        },
                    ],
                },
                "python",
            ),
        ]

        for output_format, report, expected_type in cases:
            with self.subTest(output_format=output_format):
                tool = SbomGenerateTool(
                    runner=_FakeRunner(report),
                    runtime=_FakeRuntime("syft"),
                    execution_enabled=True,
                )

                result = self._execute(
                    tool,
                    {
                        "target": "project",
                        "output_format": output_format,
                        "max_packages": 1,
                    },
                )

                self.assertTrue(result.success)
                payload = json.loads(result.output)
                self.assertEqual(payload["package_count"], 1)
                self.assertEqual(payload["packages"][0]["name"], "requests")
                self.assertEqual(payload["packages"][0]["type"], expected_type)
                self.assertEqual(payload["engine_version"], "1.51.0")
                self.assertNotIn(absolute_source, result.output)


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
                    "status": {
                        "schemaVersion": "v6.1.9",
                        "from": "https://grype.anchore.io/databases/v6/archive.tar.zst",
                        "built": "2026-08-21T00:00:00Z",
                        "path": "/secret/cache/path",
                        "valid": True,
                    },
                    "providers": {},
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
        self.assertEqual(
            payload["database"],
            {
                "built": "2026-08-21T00:00:00Z",
                "schema_version": "v6.1.9",
                "valid": True,
            },
        )
        self.assertEqual(result.metadata["database_built"], "2026-08-21T00:00:00Z")
        self.assertNotIn("/secret/cache/path", result.output)
        self.assertNotIn("grype.anchore.io", result.output)
        self.assertLessEqual(len(payload["findings"][0]["description"]), 1200)
        call = runner.calls[0]
        self.assertEqual(call["args"][call["args"].index("--from") + 1], "sbom")
        self.assertIn("--only-fixed", call["args"])
        self.assertNotIn("--quiet", call["args"])
        self.assertEqual(call["environment"]["GRYPE_DB_AUTO_UPDATE"], "false")

    def test_rejects_report_without_matches_array(self) -> None:
        reports = [
            ("missing", {"descriptor": {}}),
            ("null", {"descriptor": {}, "matches": None}),
            ("object", {"descriptor": {}, "matches": {}}),
        ]
        for label, report in reports:
            with self.subTest(report=label):
                tool = VulnerabilityScanTool(
                    runner=_FakeRunner(report),
                    runtime=_FakeRuntime("grype"),
                    execution_enabled=True,
                )

                result = self._execute(tool, {"target": "project"})

                self.assertFalse(result.success)
                self.assertIn("required matches array", result.error)

    def test_rejects_non_object_match_entry(self) -> None:
        report = {
            "descriptor": {},
            "matches": [self._match("CVE-1", "High", "pkg"), None],
        }
        tool = VulnerabilityScanTool(
            runner=_FakeRunner(report),
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertFalse(result.success)
        self.assertIn("match at index 1 must be an object", result.error)

    def test_accepts_empty_matches_array_as_clean_scan(self) -> None:
        tool = VulnerabilityScanTool(
            runner=_FakeRunner({"descriptor": {}, "matches": []}),
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(tool, {"target": "project"})

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["match_count"], 0)
        self.assertEqual(payload["findings"], [])

    def test_rejects_plain_file_source_before_runtime(self) -> None:
        runtime = _FakeRuntime("grype")
        runner = _FakeRunner()
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {"target": "project/requirements.txt", "source_type": "file"},
        )

        self.assertFalse(result.success)
        self.assertIn("does not scan plain files", result.error)
        self.assertEqual(runtime.calls, 0)
        self.assertEqual(runner.calls, [])

    def test_report_too_large_to_summarize_keeps_artifact(self) -> None:
        tool = VulnerabilityScanTool(
            runner=_FakeRunner({"descriptor": {}, "matches": []}),
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        with patch("kerkov.tools.supply_chain_common.MAX_SUMMARY_REPORT_BYTES", 10):
            result = self._execute(tool, {"target": "project"})

        self.assertFalse(result.success)
        self.assertIn("too large to summarize", result.error)
        payload = json.loads(result.output)
        self.assertTrue(Path(payload["artifacts"]["report"]).is_file())

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

    def test_auto_image_uses_only_explicit_image_sources(self) -> None:
        runner = _FakeRunner({"descriptor": {}, "matches": []})
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=_FakeRuntime("grype"),
            execution_enabled=True,
        )

        result = self._execute(
            tool,
            {"target": "alpine:3.22", "source_type": "image"},
        )

        self.assertTrue(result.success)
        self.assertEqual(
            _from_values(runner.calls[0]["args"]),
            ["docker", "podman", "containerd", "registry"],
        )

    def test_rejects_non_image_sources_and_paths_before_runtime(self) -> None:
        runtime = _FakeRuntime("grype")
        runner = _FakeRunner()
        tool = VulnerabilityScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )

        for target in ("dir:/etc", "file:/etc/passwd", "/etc", "C:\\Windows"):
            with self.subTest(target=target):
                result = self._execute(
                    tool,
                    {"target": target, "source_type": "image"},
                )
                self.assertFalse(result.success)
                self.assertIn("image target", result.error)

        self.assertEqual(runtime.calls, 0)
        self.assertEqual(runner.calls, [])

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
