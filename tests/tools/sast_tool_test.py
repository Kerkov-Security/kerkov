"""Tests for the workspace-local OpenGrep SAST adapter."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

from kerkov.core.opengrep_runtime import (
    OpenGrepRuntimeError,
    OpenGrepRuntimeInfo,
)
from kerkov.tools.sast_tool import (
    OpenGrepCommandResult,
    SastScanTool,
    SubprocessOpenGrepCommandRunner,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _empty_report() -> Dict[str, Any]:
    return {
        "version": "1.22.0",
        "results": [],
        "errors": [],
        "paths": {"scanned": []},
    }


class _FakeOpenGrepRunner:
    def __init__(
        self,
        *,
        report: Optional[Dict[str, Any]] = None,
        result: Optional[OpenGrepCommandResult] = None,
        write_report: bool = True,
        log_text: str = "",
    ) -> None:
        self.report = report if report is not None else _empty_report()
        self.result = result or OpenGrepCommandResult(exit_code=0)
        self.write_report = write_report
        self.log_text = log_text
        self.calls: List[List[str]] = []
        self.working_directories: List[Path] = []
        self.timeouts: List[float] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
    ) -> OpenGrepCommandResult:
        command = list(args)
        self.calls.append(command)
        self.working_directories.append(cwd)
        self.timeouts.append(timeout_seconds)
        log_path.write_text(self.log_text, encoding="utf-8")
        if self.write_report:
            output_index = command.index("--output") + 1
            Path(command[output_index]).write_text(
                json.dumps(self.report), encoding="utf-8"
            )
        return self.result


class _FakeOpenGrepRuntime:
    def __init__(
        self,
        *,
        info: Optional[OpenGrepRuntimeInfo] = None,
        error: Optional[str] = None,
        delay_seconds: float = 0,
    ) -> None:
        self.info = info or OpenGrepRuntimeInfo(
            binary="/managed/opengrep",
            source="kerkov_install",
            version="v9.9.9",
            installed=True,
        )
        self.error = error
        self.delay_seconds = delay_seconds
        self.calls: List[tuple[bool, float]] = []

    async def resolve(
        self,
        *,
        install_if_missing: bool,
        timeout_seconds: float,
    ) -> OpenGrepRuntimeInfo:
        self.calls.append((install_if_missing, timeout_seconds))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise OpenGrepRuntimeError(self.error)
        return self.info


class _SastScanToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.source = self.workspace / "src"
        self.source.mkdir()
        (self.source / "app.py").write_text("print('hello')\n", encoding="utf-8")
        self.rules = self.workspace / "rules.yml"
        self.rules.write_text(
            "rules:\n"
            "  - id: demo\n"
            "    pattern: print(...)\n"
            "    message: demo\n"
            "    languages: [python]\n"
            "    severity: INFO\n",
            encoding="utf-8",
        )

    def _execute(
        self,
        runner: _FakeOpenGrepRunner,
        params: Optional[Dict[str, Any]] = None,
        *,
        execution_enabled: bool = True,
    ) -> Any:
        tool = SastScanTool(
            runner=runner,
            execution_enabled=execution_enabled,
            binary="opengrep",
        )
        request = params or {"target": "src", "config_paths": ["rules.yml"]}
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            return _run(tool.execute(request, call_id="call/sast"))


class SastExecutionPolicyTest(_SastScanToolTestBase):
    def test_disabled_by_default_before_runner_or_artifact_side_effects(self) -> None:
        runner = _FakeOpenGrepRunner()
        tool = SastScanTool(runner=runner)
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            result = _run(
                tool.execute(
                    {"target": "src", "config_paths": ["rules.yml"]},
                    call_id="call/sast",
                )
            )

        self.assertFalse(result.success)
        self.assertIn("EXECUTION_ENABLED=true", result.error or "")
        self.assertEqual(
            result.metadata,
            {"policy": "execution_disabled", "engine": "opengrep"},
        )
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_enabled_scan_invokes_runner(self) -> None:
        runner = _FakeOpenGrepRunner()
        result = self._execute(runner)

        self.assertTrue(result.success)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.working_directories, [self.workspace])
        self.assertEqual(len(runner.timeouts), 1)
        self.assertGreater(runner.timeouts[0], 179)
        self.assertLessEqual(runner.timeouts[0], 180)


class SastRuntimeBootstrapTest(_SastScanToolTestBase):
    def test_default_scan_allows_latest_stable_install(self) -> None:
        runner = _FakeOpenGrepRunner()
        runtime = _FakeOpenGrepRuntime()
        tool = SastScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            result = _run(
                tool.execute(
                    {"target": "src", "config_paths": ["rules.yml"]},
                    call_id="call/sast",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(runtime.calls), 1)
        self.assertTrue(runtime.calls[0][0])
        self.assertGreater(runtime.calls[0][1], 179)
        self.assertLessEqual(runtime.calls[0][1], 180)
        self.assertEqual(runner.calls[0][0], "/managed/opengrep")
        payload = json.loads(result.output)
        self.assertEqual(
            payload["runtime"],
            {
                "installed": True,
                "source": "kerkov_install",
                "version": "v9.9.9",
            },
        )
        self.assertTrue(result.metadata["runtime_installed"])

    def test_install_can_be_disabled_per_scan(self) -> None:
        runner = _FakeOpenGrepRunner()
        runtime = _FakeOpenGrepRuntime(error="no usable binary")
        tool = SastScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            result = _run(
                tool.execute(
                    {
                        "target": "src",
                        "config_paths": ["rules.yml"],
                        "install_if_missing": False,
                    },
                    call_id="call/sast",
                )
            )

        self.assertFalse(result.success)
        self.assertEqual(len(runtime.calls), 1)
        self.assertFalse(runtime.calls[0][0])
        self.assertIn("Could not prepare the OpenGrep runtime", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_runtime_setup_and_scan_share_one_timeout_budget(self) -> None:
        runner = _FakeOpenGrepRunner()
        runtime = _FakeOpenGrepRuntime(delay_seconds=0.05)
        tool = SastScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            result = _run(
                tool.execute(
                    {
                        "target": "src",
                        "config_paths": ["rules.yml"],
                        "timeout_seconds": 1,
                    },
                    call_id="call/sast",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(len(runtime.calls), 1)
        setup_budget = runtime.calls[0][1]
        self.assertGreater(setup_budget, 0)
        self.assertLessEqual(setup_budget, 1)
        self.assertEqual(len(runner.timeouts), 1)
        self.assertGreater(runner.timeouts[0], 0)
        self.assertLess(runner.timeouts[0], setup_budget)

    def test_runtime_setup_cannot_exceed_total_timeout(self) -> None:
        runner = _FakeOpenGrepRunner()
        runtime = _FakeOpenGrepRuntime(delay_seconds=2)
        tool = SastScanTool(
            runner=runner,
            runtime=runtime,
            execution_enabled=True,
        )
        with patch(
            "kerkov.tools.sast_tool.get_current_workspace",
            return_value=self.workspace,
        ):
            result = _run(
                tool.execute(
                    {
                        "target": "src",
                        "config_paths": ["rules.yml"],
                        "timeout_seconds": 1,
                    },
                    call_id="call/sast",
                )
            )

        self.assertFalse(result.success)
        self.assertIn(
            "timed out after 1 seconds during runtime setup", result.error or ""
        )
        self.assertEqual(runner.calls, [])


class SastWorkspaceBoundaryTest(_SastScanToolTestBase):
    def test_target_cannot_escape_workspace(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {"target": str(outside), "config_paths": ["rules.yml"]},
        )

        self.assertFalse(result.success)
        self.assertIn("target must stay under workspace", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_config_cannot_escape_workspace(self) -> None:
        outside_rules = self.root / "outside.yml"
        outside_rules.write_text("rules: []\n", encoding="utf-8")
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {"target": "src", "config_paths": [str(outside_rules)]},
        )

        self.assertFalse(result.success)
        self.assertIn("config path must stay under workspace", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_target_symlink_cannot_escape_workspace(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "escaped"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {"target": "escaped", "config_paths": ["rules.yml"]},
        )

        self.assertFalse(result.success)
        self.assertIn("target must stay under workspace", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_artifact_symlink_cannot_escape_workspace(self) -> None:
        outside = self.root / "outside-artifacts"
        outside.mkdir()
        try:
            (self.workspace / "artifacts").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        runner = _FakeOpenGrepRunner()

        result = self._execute(runner)

        self.assertFalse(result.success)
        self.assertIn("artifact directory resolves outside", result.error or "")
        self.assertEqual(runner.calls, [])
        self.assertFalse((outside / "sast").exists())

    def test_missing_target_is_rejected_before_artifact_creation(self) -> None:
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {"target": "missing", "config_paths": ["rules.yml"]},
        )

        self.assertFalse(result.success)
        self.assertIn("target does not exist", result.error or "")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.workspace / "artifacts").exists())


class SastConfigurationTest(_SastScanToolTestBase):
    def test_discovers_workspace_opengrep_config(self) -> None:
        discovered = self.workspace / ".opengrep.yml"
        discovered.write_text("rules: []\n", encoding="utf-8")
        runner = _FakeOpenGrepRunner()

        result = self._execute(runner, {"target": "src"})

        self.assertTrue(result.success)
        command = runner.calls[0]
        config_index = command.index("--config") + 1
        self.assertEqual(Path(command[config_index]), discovered)

    def test_requires_local_config_when_none_is_discovered(self) -> None:
        runner = _FakeOpenGrepRunner()

        result = self._execute(runner, {"target": "src"})

        self.assertFalse(result.success)
        self.assertIn("no local OpenGrep configuration", result.error or "")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_discovered_config_symlink_cannot_escape_workspace(self) -> None:
        outside_rules = self.root / "outside.yml"
        outside_rules.write_text("rules: []\n", encoding="utf-8")
        try:
            (self.workspace / ".opengrep.yml").symlink_to(outside_rules)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        runner = _FakeOpenGrepRunner()

        result = self._execute(runner, {"target": "src"})

        self.assertFalse(result.success)
        self.assertIn("discovered config path must stay", result.error or "")
        self.assertEqual(runner.calls, [])

    def test_command_uses_only_supported_structured_options(self) -> None:
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {
                "target": "src",
                "config_paths": ["rules.yml"],
                "severities": ["ERROR", "WARNING"],
                "include": ["src/**", "--autofix"],
                "exclude": ["**/test/**"],
                "intrafile_taint": True,
                "timeout_seconds": 42,
            },
        )

        self.assertTrue(result.success)
        command = runner.calls[0]
        self.assertEqual(
            command[:5], ["opengrep", "scan", "--quiet", "--json", "--output"]
        )
        self.assertEqual(command[-1], str(self.source))
        self.assertEqual(command.count("--config"), 1)
        self.assertEqual(command.count("--severity"), 2)
        self.assertIn("--include=src/**", command)
        self.assertIn("--include=--autofix", command)
        self.assertIn("--exclude=**/test/**", command)
        self.assertIn("--force-exclude", command)
        self.assertIn("--taint-intrafile", command)
        self.assertNotIn("--autofix", command)
        self.assertNotIn("--config=auto", command)
        self.assertEqual(len(runner.timeouts), 1)
        self.assertGreater(runner.timeouts[0], 41)
        self.assertLessEqual(runner.timeouts[0], 42)

    def test_duplicate_severities_are_rejected(self) -> None:
        runner = _FakeOpenGrepRunner()

        result = self._execute(
            runner,
            {
                "target": "src",
                "config_paths": ["rules.yml"],
                "severities": ["ERROR", "ERROR"],
            },
        )

        self.assertFalse(result.success)
        self.assertIn("must not contain duplicates", result.error or "")
        self.assertEqual(runner.calls, [])


class SastReportTest(_SastScanToolTestBase):
    def _finding(
        self,
        rule_id: str,
        severity: str,
        path: str,
        line: int,
    ) -> Dict[str, Any]:
        return {
            "check_id": rule_id,
            "path": path,
            "start": {"line": line, "col": 2},
            "end": {"line": line, "col": 8},
            "extra": {
                "severity": severity,
                "message": f"{severity.lower()} finding",
                "lines": "SECRET_SOURCE_SNIPPET",
                "metavars": {"$VALUE": {"abstract_content": "SECRET_VALUE"}},
                "metadata": {
                    "cwe": ["CWE-78"],
                    "owasp": ["A03:2021"],
                    "confidence": "HIGH",
                    "technology": ["python"],
                },
            },
        }

    def test_returns_bounded_summary_and_keeps_complete_raw_report(self) -> None:
        report = {
            "version": "1.22.0",
            "results": [
                self._finding("info.rule", "INFO", "../private.py", 30),
                self._finding("warn.rule", "WARNING", "src/warn.py", 20),
                self._finding("error.rule", "ERROR", "src/error.py", 10),
            ],
            "errors": [],
            "paths": {"scanned": ["src/app.py", "src/other.py"]},
        }
        runner = _FakeOpenGrepRunner(report=report)

        result = self._execute(
            runner,
            {
                "target": "src",
                "config_paths": ["rules.yml"],
                "max_findings": 2,
            },
        )

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["finding_count"], 3)
        self.assertEqual(payload["returned_finding_count"], 2)
        self.assertTrue(payload["findings_truncated"])
        self.assertEqual(
            payload["counts_by_severity"],
            {"ERROR": 1, "INFO": 1, "WARNING": 1},
        )
        self.assertEqual(payload["scanned_path_count"], 2)
        self.assertEqual(
            [finding["rule_id"] for finding in payload["findings"]],
            ["error.rule", "warn.rule"],
        )
        self.assertEqual(payload["findings"][0]["path"], "src/error.py")
        self.assertNotIn("SECRET_SOURCE_SNIPPET", result.output)
        self.assertNotIn("SECRET_VALUE", result.output)

        report_path = Path(payload["artifacts"]["report"])
        self.assertIn("SECRET_SOURCE_SNIPPET", report_path.read_text(encoding="utf-8"))
        self.assertEqual(result.metadata["finding_count"], 3)
        self.assertTrue(result.metadata["findings_truncated"])
        self.assertTrue(Path(result.metadata["log_artifact"]).is_file())

    def test_findings_are_a_success_even_with_scanner_diagnostics(self) -> None:
        report = _empty_report()
        report["results"] = [self._finding("error.rule", "ERROR", "src/app.py", 1)]
        report["errors"] = [{"message": "one file could not be parsed"}]
        runner = _FakeOpenGrepRunner(report=report)

        result = self._execute(runner)

        self.assertTrue(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "completed_with_errors")
        self.assertEqual(payload["scanner_errors"], ["one file could not be parsed"])
        self.assertEqual(payload["finding_count"], 1)

    def test_missing_results_array_is_rejected(self) -> None:
        runner = _FakeOpenGrepRunner(report={"version": "1.22.0"})

        result = self._execute(runner)

        self.assertFalse(result.success)
        self.assertIn("results array", result.error or "")

    def test_empty_report_is_rejected(self) -> None:
        runner = _FakeOpenGrepRunner(write_report=False)

        result = self._execute(runner)

        self.assertFalse(result.success)
        self.assertIn("without creating its JSON report", result.error or "")

    def test_missing_binary_has_actionable_failure(self) -> None:
        runner = _FakeOpenGrepRunner(
            result=OpenGrepCommandResult(
                exit_code=None,
                error="Failed to start 'opengrep': executable not found",
            ),
            write_report=False,
        )

        result = self._execute(runner)

        self.assertFalse(result.success)
        self.assertIn("Install the open-source OpenGrep binary", result.error or "")
        self.assertEqual(result.metadata["status"], "failed")

    def test_failure_console_output_is_bounded_to_log_tail(self) -> None:
        runner = _FakeOpenGrepRunner(
            result=OpenGrepCommandResult(exit_code=2),
            write_report=False,
            log_text="x" * 10_000 + "FINAL DIAGNOSTIC",
        )

        result = self._execute(runner)

        self.assertFalse(result.success)
        payload = json.loads(result.output)
        self.assertLessEqual(len(payload["diagnostic_tail"]), 4_000)
        self.assertTrue(payload["diagnostic_tail"].endswith("FINAL DIAGNOSTIC"))


class SubprocessOpenGrepRunnerTest(unittest.TestCase):
    def test_output_streams_to_log_with_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "opengrep.log"
            runner = SubprocessOpenGrepCommandRunner()
            code = (
                "import os; "
                "print(os.getenv('OPENAI_API_KEY'), "
                "os.getenv('SEMGREP_APP_TOKEN'), "
                "os.getenv('SAST_TEST_MARKER'), "
                "os.getenv('SEMGREP_SEND_METRICS'), "
                "os.getenv('SEMGREP_ENABLE_VERSION_CHECK'))"
            )
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "must-not-leak",
                    "SEMGREP_APP_TOKEN": "must-also-not-leak",
                    "SAST_TEST_MARKER": "present",
                },
            ):
                result = _run(
                    runner.run(
                        [sys.executable, "-c", code],
                        cwd=root,
                        timeout_seconds=5,
                        log_path=log_path,
                    )
                )

            log = log_path.read_text(encoding="utf-8")
            self.assertTrue(result.success)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("must-not-leak", log)
            self.assertNotIn("must-also-not-leak", log)
            self.assertIn("None None present off 0", log)

    def test_timeout_kills_process_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "opengrep.log"
            runner = SubprocessOpenGrepCommandRunner()

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
            self.assertIn("timed out", log_path.read_text(encoding="utf-8"))

    def test_missing_executable_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "opengrep.log"
            runner = SubprocessOpenGrepCommandRunner()

            result = _run(
                runner.run(
                    ["definitely-not-a-real-opengrep-binary"],
                    cwd=root,
                    timeout_seconds=5,
                    log_path=log_path,
                )
            )

            self.assertFalse(result.success)
            self.assertIsNone(result.exit_code)
            self.assertIn("Failed to start", result.error or "")


if __name__ == "__main__":
    unittest.main()
