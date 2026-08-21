"""Low-noise, local SAST scanning backed by OpenGrep.

The tool deliberately accepts only workspace-local scan targets and rule
configurations.  It invokes OpenGrep without a shell, stores the complete JSON
report in the engagement workspace, and returns a bounded normalized summary
to the model.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

# OpenGrep is executed as a fixed argument vector without a shell.
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Type

from pydantic import Field

from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.opengrep_runtime import (
    OpenGrepInstaller,
    OpenGrepRuntimeError,
    OpenGrepRuntimeInfo,
    OpenGrepRuntimeResolver,
)
from kerkov.core.process.kill_tree import kill_process_tree
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

DEFAULT_SCAN_TIMEOUT_SECONDS = 180
DEFAULT_MAX_FINDINGS = 50
MAX_FINDINGS = 200
MAX_CONFIG_PATHS = 20
MAX_FILTER_PATTERNS = 50
MAX_PATTERN_CHARS = 512
MAX_MESSAGE_CHARS = 1200
MAX_RULE_ID_CHARS = 300
MAX_METADATA_ITEMS = 10
MAX_SCANNER_ERRORS = 10
MAX_ERROR_CHARS = 1200
MAX_FAILURE_TAIL_CHARS = 4000

_AUTO_CONFIG_FILENAMES = (
    ".opengrep.yml",
    ".opengrep.yaml",
    "opengrep.yml",
    "opengrep.yaml",
    ".semgrep.yml",
    ".semgrep.yaml",
    "semgrep.yml",
    "semgrep.yaml",
)

_SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}


def _subprocess_group_options() -> Dict[str, Any]:
    """Put OpenGrep in its own process group so workers can be torn down."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Best-effort, bounded teardown for OpenGrep and any worker processes."""
    if process.returncode is not None:
        return
    kill_process_tree(process.pid, grace_ms=0)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        await process.wait()


class SastSeverity(str, Enum):
    """OpenGrep severities accepted by the scan filter."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SastScanParams(ToolParams):
    target: str = Field(
        default=".",
        description=(
            "Workspace-local file or directory to scan. Relative paths are "
            "resolved from the active engagement workspace."
        ),
        max_length=4096,
    )
    config_paths: List[str] = Field(
        default_factory=list,
        description=(
            "Workspace-local OpenGrep rule file or directory paths. When "
            "omitted, the tool looks for a standard OpenGrep/Semgrep YAML "
            "configuration beside the target and at the workspace root. "
            "Registry names and URLs are not accepted."
        ),
        max_length=MAX_CONFIG_PATHS,
    )
    severities: List[SastSeverity] = Field(
        default_factory=list,
        description=(
            "Optional severities to report: INFO, WARNING, and/or ERROR. "
            "Omit to report all severities."
        ),
        max_length=3,
    )
    include: List[str] = Field(
        default_factory=list,
        description="Optional gitignore-style include patterns.",
        max_length=MAX_FILTER_PATTERNS,
    )
    exclude: List[str] = Field(
        default_factory=list,
        description="Optional gitignore-style exclude patterns.",
        max_length=MAX_FILTER_PATTERNS,
    )
    intrafile_taint: bool = Field(
        default=True,
        description=(
            "Enable OpenGrep's open intrafile/interprocedural taint analysis."
        ),
    )
    install_if_missing: bool = Field(
        default=True,
        description=(
            "Allow Kerkov to download the latest stable, SHA-256-verified "
            "OpenGrep binary into the current user's ~/.kerkov/tools directory "
            "when no usable opengrep executable is available. No account or paid "
            "service is used."
        ),
    )
    timeout_seconds: int = Field(
        default=DEFAULT_SCAN_TIMEOUT_SECONDS,
        description="Maximum total scan runtime in seconds.",
        ge=1,
        le=1800,
    )
    max_findings: int = Field(
        default=DEFAULT_MAX_FINDINGS,
        description=(
            "Maximum findings returned to the model. The complete report is "
            "always retained as a workspace artifact."
        ),
        ge=1,
        le=MAX_FINDINGS,
    )


@dataclass
class OpenGrepCommandResult:
    """Result of invoking the OpenGrep executable."""

    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


class OpenGrepCommandRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
    ) -> OpenGrepCommandResult:
        """Run OpenGrep without a shell and persist its console output."""
        ...


class SubprocessOpenGrepCommandRunner:
    """Execute OpenGrep as an argument vector, never through a shell."""

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        log_path: Path,
    ) -> OpenGrepCommandResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = sanitized_subprocess_environment()
        # A local OpenGrep scan never needs Semgrep's commercial API token,
        # even if the parent shell happens to define one.
        environment.pop("SEMGREP_APP_TOKEN", None)
        # OpenGrep does not need network access for the local configs accepted
        # by this tool. These inherited Semgrep compatibility settings also
        # suppress registry telemetry/version checks if a compatible build
        # consults them.
        environment.update(
            {
                "SEMGREP_SEND_METRICS": "off",
                "SEMGREP_ENABLE_VERSION_CHECK": "0",
            }
        )

        with log_path.open("ab") as log_handle:
            command_record = json.dumps(list(args), ensure_ascii=True)
            log_handle.write(("$ argv=" + command_record + "\n").encode("utf-8"))
            log_handle.flush()
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(cwd),
                    env=environment,
                    stdout=log_handle,
                    stderr=log_handle,
                    **_subprocess_group_options(),
                )
            except OSError as exc:
                message = f"Failed to start {args[0]!r}: {exc}"
                log_handle.write((message + "\n").encode("utf-8", errors="replace"))
                return OpenGrepCommandResult(exit_code=None, error=message)

            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                await _terminate_process_tree(process)
                log_handle.write(b"\nOpenGrep scan interrupted by user\n")
                raise
            except asyncio.TimeoutError:
                await _terminate_process_tree(process)
                message = f"OpenGrep scan timed out after {timeout_seconds:g} seconds"
                log_handle.write(("\n" + message + "\n").encode("utf-8"))
                return OpenGrepCommandResult(
                    exit_code=process.returncode,
                    timed_out=True,
                    error=message,
                )

            return OpenGrepCommandResult(exit_code=process.returncode)


class SastScanTool(BaseTool):
    """Run an OpenGrep SAST scan against local engagement source code."""

    def __init__(
        self,
        runner: Optional[OpenGrepCommandRunner] = None,
        *,
        execution_enabled: bool = False,
        binary: Optional[str] = None,
        runtime: Optional[OpenGrepRuntimeResolver] = None,
    ) -> None:
        super().__init__(
            name="sast_scan",
            description=(
                "Scan source code for security vulnerabilities with the "
                "open-source OpenGrep engine. If no usable executable exists, "
                "the approved call can install the latest stable, "
                "SHA-256-verified official binary in the current user's "
                "~/.kerkov/tools directory. Targets and rule configurations must "
                "stay inside the active engagement workspace; remote rulesets, "
                "URLs, arbitrary scanner flags, builds, and autofix are "
                "intentionally unsupported. The full JSON report is saved as an "
                "artifact while a compact severity-sorted summary is returned. "
                "Findings mean the scan succeeded, not that the tool failed. "
                "Prefer this over exec for OpenGrep scans."
            ),
        )
        self.execution_enabled = execution_enabled
        self._runner = runner or SubprocessOpenGrepCommandRunner()
        self._binary = binary
        self._runtime = runtime or OpenGrepInstaller()

    def get_params_class(self) -> Type[ToolParams]:
        return SastScanParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        if not isinstance(toolParams, SastScanParams):
            return "invalid SAST scan parameters"
        if not toolParams.target.strip():
            return "target must not be empty"
        if "\x00" in toolParams.target:
            return "target must not contain NUL bytes"

        for field_name, values in (
            ("config_paths", toolParams.config_paths),
            ("include", toolParams.include),
            ("exclude", toolParams.exclude),
        ):
            for value in values:
                if not value.strip():
                    return f"{field_name} entries must not be empty"
                if "\x00" in value:
                    return f"{field_name} entries must not contain NUL bytes"
                if field_name == "config_paths" and len(value) > 4096:
                    return "config_paths entries must be at most 4096 characters"
                if field_name != "config_paths" and len(value) > MAX_PATTERN_CHARS:
                    return (
                        f"{field_name} entries must be at most "
                        f"{MAX_PATTERN_CHARS} characters"
                    )
        if len(set(toolParams.severities)) != len(toolParams.severities):
            return "severities must not contain duplicates"
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, SastScanParams):
            raise TypeError("expected SastScanParams")
        if not self.execution_enabled:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Process execution is disabled. Set EXECUTION_ENABLED=true "
                    "to enable the sast_scan tool."
                ),
                metadata={"policy": "execution_disabled", "engine": "opengrep"},
            )

        workspace = (get_current_workspace() or Path.cwd()).resolve()
        try:
            target = self._resolve_workspace_path(
                toolParams.target, workspace, label="target"
            )
        except ValueError as exc:
            return self._validation_failure(str(exc))
        if not target.exists():
            return self._validation_failure(f"target does not exist: {target}")
        if not (target.is_file() or target.is_dir()):
            return self._validation_failure(
                f"target must be a regular file or directory: {target}"
            )

        try:
            configs = self._resolve_configs(toolParams, target, workspace)
        except ValueError as exc:
            return self._validation_failure(str(exc))

        try:
            report_path, log_path = self._new_artifact_paths(workspace)
        except (OSError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Could not prepare SAST artifacts: {exc}",
                metadata={"policy": "workspace_local", "engine": "opengrep"},
            )
        try:
            runtime = await self._resolve_runtime(toolParams, log_path)
        except OpenGrepRuntimeError as exc:
            return self._command_failure(
                OpenGrepCommandResult(
                    exit_code=None,
                    error=f"Could not prepare the OpenGrep runtime: {exc}",
                ),
                target=target,
                report_path=report_path,
                log_path=log_path,
            )

        args = self._build_args(
            toolParams,
            target,
            configs,
            report_path,
            binary=runtime.binary,
        )
        command_result = await self._runner.run(
            args,
            cwd=workspace,
            timeout_seconds=toolParams.timeout_seconds,
            log_path=log_path,
        )
        if not command_result.success:
            return self._command_failure(
                command_result,
                target=target,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )

        try:
            report_is_populated = (
                report_path.is_file() and report_path.stat().st_size > 0
            )
        except OSError:
            report_is_populated = False
        if not report_is_populated:
            return self._report_failure(
                "OpenGrep completed without creating its JSON report",
                target=target,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )
        try:
            with report_path.open("r", encoding="utf-8") as report_file:
                report = json.load(report_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return self._report_failure(
                f"Could not parse OpenGrep JSON report: {exc}",
                target=target,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )
        if not isinstance(report, dict):
            return self._report_failure(
                "OpenGrep JSON report must contain an object",
                target=target,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )
        if not isinstance(report.get("results"), list):
            return self._report_failure(
                "OpenGrep JSON report must contain a results array",
                target=target,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )

        payload = self._normalize_report(
            report,
            params=toolParams,
            target=target,
            configs=configs,
            workspace=workspace,
            report_path=report_path,
            log_path=log_path,
            runtime=runtime,
        )
        return ToolResult(
            success=True,
            output=json.dumps(payload, indent=2, sort_keys=True),
            metadata=self._compact_metadata(payload),
        )

    @staticmethod
    def _validation_failure(message: str) -> ToolResult:
        return ToolResult(
            success=False,
            output="",
            error=f"Invalid SAST scan request: {message}",
            metadata={"policy": "workspace_local", "engine": "opengrep"},
        )

    @staticmethod
    def _resolve_workspace_path(raw_path: str, workspace: Path, *, label: str) -> Path:
        requested = Path(raw_path).expanduser()
        path = (
            requested.resolve()
            if requested.is_absolute()
            else (workspace / requested).resolve()
        )
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"{label} must stay under workspace: {workspace}") from exc
        return path

    def _resolve_configs(
        self,
        params: SastScanParams,
        target: Path,
        workspace: Path,
    ) -> List[Path]:
        if params.config_paths:
            configs: List[Path] = []
            for raw_path in params.config_paths:
                path = self._resolve_workspace_path(
                    raw_path, workspace, label="config path"
                )
                if not path.exists():
                    raise ValueError(f"config path does not exist: {path}")
                if not (path.is_file() or path.is_dir()):
                    raise ValueError(f"config path must be a file or directory: {path}")
                configs.append(path)
            return self._deduplicate_paths(configs)

        search_roots = [target if target.is_dir() else target.parent, workspace]
        discovered: List[Path] = []
        for root in self._deduplicate_paths(search_roots):
            for filename in _AUTO_CONFIG_FILENAMES:
                candidate = root / filename
                if candidate.is_file():
                    discovered.append(
                        self._resolve_workspace_path(
                            str(candidate),
                            workspace,
                            label="discovered config path",
                        )
                    )
        discovered = self._deduplicate_paths(discovered)
        if discovered:
            return discovered

        expected = ", ".join(_AUTO_CONFIG_FILENAMES)
        raise ValueError(
            "no local OpenGrep configuration was supplied or discovered; "
            f"provide config_paths or add one of: {expected}"
        )

    @staticmethod
    def _deduplicate_paths(paths: Sequence[Path]) -> List[Path]:
        result: List[Path] = []
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        return result

    def _new_artifact_paths(self, workspace: Path) -> tuple[Path, Path]:
        root = (workspace / "artifacts" / "sast").resolve(strict=False)
        try:
            root.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(
                "workspace artifact directory resolves outside the workspace"
            ) from exc
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        try:
            root.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(
                "workspace artifact directory resolves outside the workspace"
            ) from exc
        raw_id = self.execution_call_id or "scan"
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_id)[:60] or "scan"

        report_fd, report_name = tempfile.mkstemp(
            prefix=f"opengrep-{safe_id}-", suffix=".json", dir=str(root)
        )
        os.close(report_fd)
        log_fd, log_name = tempfile.mkstemp(
            prefix=f"opengrep-{safe_id}-", suffix=".log", dir=str(root)
        )
        os.close(log_fd)
        return Path(report_name), Path(log_name)

    def _build_args(
        self,
        params: SastScanParams,
        target: Path,
        configs: Sequence[Path],
        report_path: Path,
        *,
        binary: str,
    ) -> List[str]:
        args = [
            binary,
            "scan",
            "--quiet",
            "--json",
            "--output",
            str(report_path),
        ]
        for config in configs:
            args.extend(["--config", str(config)])
        for severity in params.severities:
            args.extend(["--severity", severity.value])
        for pattern in params.include:
            args.append(f"--include={pattern}")
        for pattern in params.exclude:
            args.append(f"--exclude={pattern}")
        if params.include or params.exclude:
            args.append("--force-exclude")
        if params.intrafile_taint:
            args.append("--taint-intrafile")
        args.append(str(target))
        return args

    async def _resolve_runtime(
        self, params: SastScanParams, log_path: Path
    ) -> OpenGrepRuntimeInfo:
        if self._binary is not None:
            info = OpenGrepRuntimeInfo(
                binary=self._binary,
                source="configured",
                version=None,
            )
        else:
            info = await self._runtime.resolve(
                install_if_missing=params.install_if_missing
            )
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    "$ runtime="
                    f"{info.source} version={info.version or 'system'} "
                    f"installed={str(info.installed).lower()}\n"
                )
        except OSError:
            pass
        return info

    def _normalize_report(
        self,
        report: Dict[str, Any],
        *,
        params: SastScanParams,
        target: Path,
        configs: Sequence[Path],
        workspace: Path,
        report_path: Path,
        log_path: Path,
        runtime: OpenGrepRuntimeInfo,
    ) -> Dict[str, Any]:
        raw_results = report.get("results")
        results = raw_results if isinstance(raw_results, list) else []
        normalized = [
            self._normalize_finding(item, workspace)
            for item in results
            if isinstance(item, dict)
        ]
        normalized.sort(key=self._finding_sort_key)

        counts: Dict[str, int] = {}
        for finding in normalized:
            severity = str(finding.get("severity") or "UNKNOWN")
            counts[severity] = counts.get(severity, 0) + 1

        scanner_errors = self._normalize_errors(report.get("errors"))
        returned = normalized[: params.max_findings]
        return {
            "status": "completed_with_errors" if scanner_errors else "completed",
            "engine": "opengrep",
            "engine_version": self._truncate_text(
                str(report.get("version") or "unknown"), 100
            ),
            "runtime": self._runtime_payload(runtime),
            "target": self._display_path(target, workspace),
            "configs": [self._display_path(path, workspace) for path in configs],
            "finding_count": len(normalized),
            "returned_finding_count": len(returned),
            "findings_truncated": len(returned) < len(normalized),
            "counts_by_severity": dict(sorted(counts.items())),
            "scanned_path_count": self._scanned_path_count(report.get("paths")),
            "scanner_errors": scanner_errors,
            "findings": returned,
            "artifacts": {
                "report": str(report_path),
                "log": str(log_path),
            },
        }

    def _normalize_finding(
        self, finding: Dict[str, Any], workspace: Path
    ) -> Dict[str, Any]:
        extra = finding.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        metadata = extra.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        start = finding.get("start")
        start = start if isinstance(start, dict) else {}
        end = finding.get("end")
        end = end if isinstance(end, dict) else {}

        normalized: Dict[str, Any] = {
            "rule_id": self._truncate_text(
                str(finding.get("check_id") or finding.get("rule_id") or "unknown"),
                MAX_RULE_ID_CHARS,
            ),
            "path": self._display_reported_path(finding.get("path"), workspace),
            "start": self._position(start),
            "end": self._position(end),
            "severity": self._normalize_severity(extra.get("severity")),
            "message": self._truncate_text(
                str(extra.get("message") or ""), MAX_MESSAGE_CHARS
            ),
        }

        optional_fields = {
            "cwe": self._string_list(metadata.get("cwe")),
            "owasp": self._string_list(metadata.get("owasp")),
            "confidence": self._optional_text(metadata.get("confidence")),
            "likelihood": self._optional_text(metadata.get("likelihood")),
            "impact": self._optional_text(metadata.get("impact")),
            "technology": self._string_list(metadata.get("technology")),
            "vulnerability_class": self._string_list(
                metadata.get("vulnerability_class")
            ),
        }
        normalized.update(
            {key: value for key, value in optional_fields.items() if value}
        )
        return normalized

    @staticmethod
    def _position(value: Dict[str, Any]) -> Dict[str, int]:
        position: Dict[str, int] = {}
        for key in ("line", "col"):
            raw = value.get(key)
            if isinstance(raw, int) and raw >= 0:
                position[key] = raw
        return position

    @staticmethod
    def _normalize_severity(value: Any) -> str:
        normalized = str(value or "UNKNOWN").upper()
        return normalized if normalized in _SEVERITY_ORDER else "UNKNOWN"

    @staticmethod
    def _finding_sort_key(finding: Dict[str, Any]) -> tuple[int, str, int, str]:
        start = finding.get("start")
        line = start.get("line", 0) if isinstance(start, dict) else 0
        return (
            _SEVERITY_ORDER.get(str(finding.get("severity")), 99),
            str(finding.get("path") or ""),
            line if isinstance(line, int) else 0,
            str(finding.get("rule_id") or ""),
        )

    def _normalize_errors(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        errors: List[str] = []
        for item in value[:MAX_SCANNER_ERRORS]:
            if isinstance(item, dict):
                text = (
                    item.get("message")
                    or item.get("type")
                    or json.dumps(item, sort_keys=True)
                )
            else:
                text = item
            errors.append(self._truncate_text(str(text), MAX_ERROR_CHARS))
        if len(value) > MAX_SCANNER_ERRORS:
            errors.append(
                f"{len(value) - MAX_SCANNER_ERRORS} additional errors omitted"
            )
        return errors

    @staticmethod
    def _scanned_path_count(value: Any) -> int:
        if not isinstance(value, dict):
            return 0
        scanned = value.get("scanned")
        return len(scanned) if isinstance(scanned, list) else 0

    def _string_list(self, value: Any) -> List[str]:
        items = value if isinstance(value, list) else [value] if value else []
        return [
            self._truncate_text(str(item), 300)
            for item in items[:MAX_METADATA_ITEMS]
            if item is not None
        ]

    def _optional_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        return self._truncate_text(str(value), 100)

    @staticmethod
    def _display_path(path: Path, workspace: Path) -> str:
        try:
            return path.relative_to(workspace).as_posix() or "."
        except ValueError:
            return path.name

    def _display_reported_path(self, value: Any, workspace: Path) -> str:
        raw = str(value or "")
        if not raw:
            return "unknown"
        path = Path(raw)
        resolved = (
            path.resolve() if path.is_absolute() else (workspace / path).resolve()
        )
        return self._display_path(resolved, workspace)

    def _command_failure(
        self,
        result: OpenGrepCommandResult,
        *,
        target: Path,
        report_path: Path,
        log_path: Path,
        runtime: Optional[OpenGrepRuntimeInfo] = None,
    ) -> ToolResult:
        reason = result.error or result.stderr.strip() or result.stdout.strip()
        if result.exit_code is None and "Failed to start" in reason:
            reason = (
                f"{reason}. Install the open-source OpenGrep binary and ensure "
                "`opengrep` is available on PATH."
            )
        reason = self._truncate_text(
            reason or f"OpenGrep exited with code {result.exit_code}",
            MAX_ERROR_CHARS,
        )
        payload = {
            "status": "failed",
            "engine": "opengrep",
            "target": str(target),
            "reason": reason,
            "diagnostic_tail": self._tail_log(log_path),
            "artifacts": {
                "report": str(report_path),
                "log": str(log_path),
            },
        }
        if runtime is not None:
            payload["runtime"] = self._runtime_payload(runtime)
        return ToolResult(
            success=False,
            output=json.dumps(payload, indent=2, sort_keys=True),
            error=f"OpenGrep scan failed: {reason}",
            metadata={
                "status": "failed",
                "engine": "opengrep",
                "report_artifact": str(report_path),
                "log_artifact": str(log_path),
                "runtime_source": runtime.source if runtime is not None else None,
            },
        )

    def _report_failure(
        self,
        reason: str,
        *,
        target: Path,
        report_path: Path,
        log_path: Path,
        runtime: OpenGrepRuntimeInfo,
    ) -> ToolResult:
        result = OpenGrepCommandResult(exit_code=0, error=reason)
        return self._command_failure(
            result,
            target=target,
            report_path=report_path,
            log_path=log_path,
            runtime=runtime,
        )

    @staticmethod
    def _compact_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
        artifacts = payload.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        return {
            "status": payload.get("status"),
            "engine": "opengrep",
            "engine_version": payload.get("engine_version"),
            "finding_count": payload.get("finding_count", 0),
            "findings_truncated": payload.get("findings_truncated", False),
            "report_artifact": artifacts.get("report"),
            "log_artifact": artifacts.get("log"),
            "runtime_source": (
                payload.get("runtime", {}).get("source")
                if isinstance(payload.get("runtime"), dict)
                else None
            ),
            "runtime_installed": (
                payload.get("runtime", {}).get("installed")
                if isinstance(payload.get("runtime"), dict)
                else False
            ),
        }

    @staticmethod
    def _runtime_payload(runtime: OpenGrepRuntimeInfo) -> Dict[str, Any]:
        return {
            "source": runtime.source,
            "version": runtime.version,
            "installed": runtime.installed,
        }

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        suffix = f"... [{omitted} chars omitted]"
        keep = max(0, max_chars - len(suffix))
        return value[:keep] + suffix

    @staticmethod
    def _tail_log(log_path: Path) -> str:
        try:
            with log_path.open("rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - MAX_FAILURE_TAIL_CHARS))
                return log_file.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""
