"""Remote compliance assessment through an owned Cinc Auditor container."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Type

from pydantic import ConfigDict, Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.compliance.cinc_runtime import DockerCincRuntime, prepare_scan
from kerkov.core.compliance.reports import ReportImportError, import_report
from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ComplianceScanParams(ToolParams):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        min_length=1,
        max_length=253,
        description="SSH hostname/IP as reachable from the scanner container. host.docker.internal reaches the Docker host; localhost refers to the scanner container.",
    )
    user: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$",
        description="SSH login user on the authorized target.",
    )
    port: int = Field(default=22, ge=1, le=65535, description="Target SSH port.")
    profile: str = Field(
        min_length=1,
        max_length=4096,
        description="Reviewed self-contained local InSpec profile directory with inspec.yml. Relative paths stay workspace-confined; the complete profile is snapshotted and hashed.",
    )
    private_key_file: str = Field(
        min_length=1,
        max_length=4096,
        description="Local owner-only SSH private-key file reference, mounted read-only. No inline passwords or interactive key passphrases.",
    )
    known_hosts_file: str = Field(
        min_length=1,
        max_length=4096,
        description="Reviewed local known_hosts file reference. SSH host-key verification is mandatory; unknown/mismatched keys fail.",
    )
    inputs_file: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional reviewed local InSpec YAML input file reference. Values are not included in tool arguments.",
    )
    sudo: bool = Field(
        default=False,
        description="Request target-side sudo for assessment commands. Requires noninteractive sudo permissions.",
    )
    timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="Docker preflight, optional image pull and assessment deadline in seconds; bounded cleanup follows.",
    )
    max_controls: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Maximum controls returned, with errors/failures first. Complete evidence remains in private workspace artifacts.",
    )


class ComplianceScanTool(BaseTool):
    """Run a reviewed profile over SSH and preserve assessment evidence."""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        runtime: DockerCincRuntime | None = None,
    ) -> None:
        super().__init__(
            name="compliance_scan",
            description=(
                "Assess an authorized SSH host with Cinc Auditor in an ephemeral Docker container. "
                "Requires Docker on a POSIX controller (Linux/macOS or WSL); "
                "the target needs SSH and sufficient privileges, "
                "not Cinc, OpenSCAP, Ruby or Docker. Use a reviewed self-contained local InSpec profile, "
                "an explicit SSH private-key file and trusted known_hosts. Profiles are executable "
                "Ruby and can issue remote commands: assessment is not an enforced read-only boundary. "
                "A missing configured image is pulled; no profile/gem dependencies or target software "
                "are installed. Saves raw JSON, normalized outcomes, provenance and scanner logs in "
                "the shared engagement workspace. Control failures/skips are data, not execution "
                "failures. Remediation requires a separate approved ansible call and reassessment."
            ),
        )
        self._config = config or get_config()
        self._runtime = runtime

    def get_params_class(self) -> Type[ToolParams]:
        return ComplianceScanParams

    def approval_scope(self, params: dict[str, Any]) -> ApprovalScope:
        return ApprovalScope(
            saveable=False,
            volatile_params=frozenset({"timeout_seconds", "max_controls"}),
        )

    def validate_params(self, toolParams: ToolParams) -> str | None:
        if not isinstance(toolParams, ComplianceScanParams):
            return "invalid compliance scan parameters"
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}|::[A-Fa-f0-9:]*", toolParams.host
        ):
            return "host must be an ordinary SSH hostname or IP, without credentials or URI syntax"
        for value in (
            toolParams.profile,
            toolParams.private_key_file,
            toolParams.known_hosts_file,
            toolParams.inputs_file,
        ):
            if value is not None and (
                not value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                return (
                    "file references must be nonempty and contain no control characters"
                )
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, ComplianceScanParams):
            raise TypeError("expected ComplianceScanParams")
        params = toolParams
        if not self._config.execution_enabled:
            return ToolResult(
                success=False,
                output="",
                error="Compliance scanning is disabled. Set EXECUTION_ENABLED=true.",
                metadata={"policy": "execution_disabled"},
            )
        workspace = get_current_workspace()
        if workspace is None:
            return ToolResult(
                success=False,
                output="",
                error="Compliance scanning requires an active engagement workspace",
            )
        metadata: dict[str, Any] = {
            "status": "failed",
            "engine": "cinc",
            "target": {"host": params.host, "user": params.user, "port": params.port},
            "sudo": params.sudo,
        }
        directory: Path | None = None
        try:
            prepared = await asyncio.to_thread(
                prepare_scan,
                workspace.resolve(),
                profile=params.profile,
                private_key_file=params.private_key_file,
                known_hosts_file=params.known_hosts_file,
                inputs_file=params.inputs_file,
                host=params.host,
                user=params.user,
                port=params.port,
                sudo=params.sudo,
            )
            directory = prepared.directory
            metadata.update(
                artifact_directory=str(directory),
                log_artifact=str(directory / "scanner.log"),
                manifest_artifact=str(directory / "manifest.json"),
                profile_name=prepared.profile_name,
                profile_sha256=prepared.profile_sha256,
                container_name=prepared.container_name,
            )
            runtime = self._runtime or DockerCincRuntime(
                image=self._config.cinc_docker_image
            )
            await runtime.run(prepared, params.timeout_seconds, metadata=metadata)
            if metadata.get("exit_code") not in {0, 100, 101}:
                raise ValueError(
                    "Cinc did not complete an assessment; inspect scanner.log"
                )
            summary = await asyncio.to_thread(
                import_report,
                workspace.resolve(),
                str(directory / "output/source.json"),
                expected_profile=prepared.profile_name,
                max_controls=params.max_controls,
                directory=directory,
                evidence_status="scanned",
            )
            metadata.update(
                {
                    key: value
                    for key, value in summary.items()
                    if key not in {"controls", "notice"}
                }
            )
            success = not summary["profile_error_count"] and not metadata.get(
                "cleanup_error"
            )
            metadata["status"] = "completed" if success else "failed"
            return ToolResult(
                success=success,
                output=json.dumps({**summary, **metadata}, indent=2),
                error=(
                    None
                    if success
                    else "Assessment reported profile errors or container cleanup failed; inspect saved artifacts"
                ),
                metadata=metadata,
            )
        except asyncio.CancelledError:
            metadata["status"] = "cancelled"
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if isinstance(exc, ReportImportError):
                metadata.update(exc.metadata)
            return ToolResult(
                success=False,
                output=json.dumps(metadata),
                error=str(exc),
                metadata=metadata,
            )
        finally:
            if directory is not None:
                await asyncio.to_thread(
                    (directory / "manifest.json").write_text,
                    json.dumps(metadata, indent=2),
                    encoding="utf-8",
                )
