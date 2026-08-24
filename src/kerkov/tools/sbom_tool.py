"""Low-noise software bill of materials generation backed by Syft."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from pydantic import Field

from kerkov.core.anchore_runtime import (
    AnchoreInstaller,
    AnchoreRuntimeError,
    AnchoreRuntimeInfo,
    AnchoreRuntimeResolver,
)
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult
from kerkov.tools.supply_chain_common import (
    ImageScope,
    ImageSource,
    ResolvedSupplyChainSource,
    SupplyChainSourceType,
    append_error_log,
    append_runtime_log,
    load_json_report,
    new_artifact_paths,
    resolve_source,
    runtime_payload,
    string_list,
    tail_log,
    truncate_text,
)
from kerkov.tools.supply_chain_runner import (
    AnchoreCommandResult,
    AnchoreCommandRunner,
    SubprocessAnchoreCommandRunner,
)

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_PACKAGES = 50
MAX_PACKAGES = 200
MAX_EXCLUDES = 50
MAX_EXCLUDE_CHARS = 512


class SbomFormat(str, Enum):
    """Machine-readable SBOM formats supported by Syft and Grype."""

    SYFT_JSON = "syft-json"
    CYCLONEDX_JSON = "cyclonedx-json"
    SPDX_JSON = "spdx-json"


class SbomGenerateParams(ToolParams):
    target: str = Field(
        default=".",
        description=(
            "Workspace-local directory/file or container image reference to "
            "catalog, selected by source_type."
        ),
        max_length=4096,
    )
    source_type: SupplyChainSourceType = Field(
        default=SupplyChainSourceType.DIRECTORY,
        description="Source kind: directory, file, or image.",
    )
    image_source: ImageSource = Field(
        default=ImageSource.AUTO,
        description=(
            "For images only: auto, docker, or registry. Registry bypasses a "
            "local container runtime."
        ),
    )
    output_format: SbomFormat = Field(
        default=SbomFormat.SYFT_JSON,
        description="Artifact format: syft-json, cyclonedx-json, or spdx-json.",
    )
    scope: ImageScope = Field(
        default=ImageScope.SQUASHED,
        description="For images, catalog the squashed filesystem or all-layers.",
    )
    exclude: List[str] = Field(
        default_factory=list,
        description="Optional Syft path exclusion globs.",
        max_length=MAX_EXCLUDES,
    )
    install_if_missing: bool = Field(
        default=True,
        description=(
            "Allow Kerkov to install the latest stable official Syft archive "
            "under ~/.kerkov/tools after validating its published SHA-256."
        ),
    )
    update_tool: bool = Field(
        default=False,
        description=(
            "Download and use the latest stable official Syft release even if "
            "a working binary is already available."
        ),
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        description="Maximum Syft runtime in seconds.",
        ge=1,
        le=1800,
    )
    max_packages: int = Field(
        default=DEFAULT_MAX_PACKAGES,
        description=(
            "Maximum packages returned to the model; the complete SBOM remains "
            "in the artifact."
        ),
        ge=1,
        le=MAX_PACKAGES,
    )


class SbomGenerateTool(BaseTool):
    """Generate a reusable Syft, CycloneDX, or SPDX SBOM artifact."""

    def __init__(
        self,
        runner: Optional[AnchoreCommandRunner] = None,
        *,
        execution_enabled: bool = False,
        binary: Optional[str] = None,
        runtime: Optional[AnchoreRuntimeResolver] = None,
    ) -> None:
        super().__init__(
            name="sbom_generate",
            description=(
                "Generate a software bill of materials with the open-source "
                "Syft engine. Local paths must stay inside the active engagement "
                "workspace; container images may come from a local Docker daemon "
                "or a registry. The full Syft JSON, CycloneDX JSON, or SPDX JSON "
                "document is saved as an artifact and a bounded package summary "
                "is returned. The approved call may install the latest stable "
                "official Syft binary after validating its published SHA-256. "
                "Prefer this over exec for SBOM generation."
            ),
        )
        self.execution_enabled = execution_enabled
        self._runner = runner or SubprocessAnchoreCommandRunner()
        self._binary = binary
        self._runtime = runtime or AnchoreInstaller("syft")

    def get_params_class(self) -> Type[ToolParams]:
        return SbomGenerateParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        if not isinstance(toolParams, SbomGenerateParams):
            return "invalid SBOM generation parameters"
        if not toolParams.target.strip():
            return "target must not be empty"
        if "\x00" in toolParams.target:
            return "target must not contain NUL bytes"
        if toolParams.source_type == SupplyChainSourceType.SBOM:
            return "Syft generates SBOMs and does not accept source_type=sbom"
        if toolParams.update_tool and not toolParams.install_if_missing:
            return "update_tool requires install_if_missing=true"
        if toolParams.source_type != SupplyChainSourceType.IMAGE:
            if toolParams.scope != ImageScope.SQUASHED:
                return "scope can only be changed when source_type=image"
        for pattern in toolParams.exclude:
            if not pattern.strip():
                return "exclude entries must not be empty"
            if "\x00" in pattern:
                return "exclude entries must not contain NUL bytes"
            if len(pattern) > MAX_EXCLUDE_CHARS:
                return f"exclude entries must be at most {MAX_EXCLUDE_CHARS} characters"
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, SbomGenerateParams):
            raise TypeError("expected SbomGenerateParams")
        if not self.execution_enabled:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Process execution is disabled. Set EXECUTION_ENABLED=true "
                    "to enable SBOM generation."
                ),
                metadata={"policy": "execution_disabled", "engine": "syft"},
            )
        workspace = get_current_workspace()
        if workspace is None:
            return ToolResult(
                success=False,
                output="",
                error="SBOM generation requires an active engagement workspace",
                metadata={"policy": "workspace_required", "engine": "syft"},
            )
        workspace = workspace.resolve()
        try:
            source = resolve_source(
                toolParams.target,
                toolParams.source_type,
                workspace,
                toolParams.image_source,
            )
            report_path, log_path = new_artifact_paths(
                workspace,
                family="sbom",
                prefix="syft",
                call_id=self.execution_call_id,
                report_suffix=self._report_suffix(toolParams.output_format),
            )
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))

        try:
            runtime = await self._resolve_runtime(toolParams, log_path)
        except AnchoreRuntimeError as exc:
            append_error_log(log_path, str(exc))
            return self._failure(str(exc), source, report_path, log_path, runtime=None)
        args = self._build_args(
            toolParams,
            source,
            report_path,
            binary=runtime.binary,
        )
        result = await self._runner.run(
            args,
            cwd=workspace,
            timeout_seconds=toolParams.timeout_seconds,
            log_path=log_path,
            environment={
                "SYFT_CHECK_FOR_APP_UPDATE": "false",
            },
            display_name="Syft catalog",
        )
        if not result.success:
            return self._command_failure(result, source, report_path, log_path, runtime)
        try:
            report = load_json_report(report_path)
            payload = self._normalize_report(
                report,
                params=toolParams,
                source=source,
                report_path=report_path,
                log_path=log_path,
                runtime=runtime,
            )
        except ValueError as exc:
            return self._failure(
                str(exc), source, report_path, log_path, runtime=runtime
            )
        return ToolResult(
            success=True,
            output=json.dumps(payload, indent=2, sort_keys=True),
            metadata={
                "status": "completed",
                "engine": "syft",
                "format": toolParams.output_format.value,
                "package_count": payload["package_count"],
                "packages_truncated": payload["packages_truncated"],
                "report_artifact": str(report_path),
                "log_artifact": str(log_path),
                "runtime_source": runtime.source,
                "runtime_installed": runtime.installed,
            },
        )

    async def _resolve_runtime(
        self, params: SbomGenerateParams, log_path: Path
    ) -> AnchoreRuntimeInfo:
        if self._binary is not None:
            runtime = AnchoreRuntimeInfo(
                binary=self._binary,
                source="configured",
                version=None,
            )
        else:
            runtime = await self._runtime.resolve(
                install_if_missing=params.install_if_missing,
                force_install=params.update_tool,
            )
        append_runtime_log(log_path, runtime)
        return runtime

    @staticmethod
    def _report_suffix(output_format: SbomFormat) -> str:
        return {
            SbomFormat.SYFT_JSON: ".syft.json",
            SbomFormat.CYCLONEDX_JSON: ".cdx.json",
            SbomFormat.SPDX_JSON: ".spdx.json",
        }[output_format]

    @staticmethod
    def _build_args(
        params: SbomGenerateParams,
        source: ResolvedSupplyChainSource,
        report_path: Path,
        *,
        binary: str,
    ) -> List[str]:
        args = [binary, source.target]
        for from_value in source.from_values:
            args.extend(["--from", from_value])
        args.extend(["--output", f"{params.output_format.value}={report_path}"])
        if params.source_type == SupplyChainSourceType.IMAGE:
            args.extend(["--scope", params.scope.value])
        for pattern in params.exclude:
            args.extend(["--exclude", pattern])
        return args

    def _normalize_report(
        self,
        report: Dict[str, Any],
        *,
        params: SbomGenerateParams,
        source: ResolvedSupplyChainSource,
        report_path: Path,
        log_path: Path,
        runtime: AnchoreRuntimeInfo,
    ) -> Dict[str, Any]:
        packages = self._packages(report, params.output_format)
        packages.sort(
            key=lambda item: (
                str(item.get("type") or ""),
                str(item.get("name") or ""),
                str(item.get("version") or ""),
            )
        )
        counts: Dict[str, int] = {}
        for package in packages:
            package_type = str(package.get("type") or "unknown")
            counts[package_type] = counts.get(package_type, 0) + 1
        returned = packages[: params.max_packages]
        return {
            "status": "completed",
            "engine": "syft",
            "engine_version": self._engine_version(report, params.output_format),
            "runtime": runtime_payload(runtime),
            "source": source.display,
            "source_type": params.source_type.value,
            "format": params.output_format.value,
            "package_count": len(packages),
            "returned_package_count": len(returned),
            "packages_truncated": len(returned) < len(packages),
            "counts_by_type": dict(sorted(counts.items())),
            "packages": returned,
            "artifacts": {"report": str(report_path), "log": str(log_path)},
        }

    def _packages(
        self, report: Dict[str, Any], output_format: SbomFormat
    ) -> List[Dict[str, Any]]:
        if output_format == SbomFormat.SYFT_JSON:
            raw = report.get("artifacts")
            values = raw if isinstance(raw, list) else []
            return [
                self._normalize_syft_package(item)
                for item in values
                if isinstance(item, dict)
            ]
        if output_format == SbomFormat.CYCLONEDX_JSON:
            raw = report.get("components")
            values = raw if isinstance(raw, list) else []
            return [
                self._normalize_cyclonedx_package(item)
                for item in values
                if isinstance(item, dict)
                and str(item.get("type") or "").casefold() != "file"
            ]
        raw = report.get("packages")
        values = raw if isinstance(raw, list) else []
        return [
            self._normalize_spdx_package(item)
            for item in values
            if isinstance(item, dict)
            and str(item.get("primaryPackagePurpose") or "").casefold() != "file"
        ]

    def _normalize_syft_package(self, item: Dict[str, Any]) -> Dict[str, Any]:
        locations = item.get("locations")
        location_values = locations if isinstance(locations, list) else []
        licenses = item.get("licenses")
        license_values = licenses if isinstance(licenses, list) else []
        normalized = self._base_package(item)
        normalized["licenses"] = [
            truncate_text(
                str(
                    license_item.get("spdxExpression")
                    or license_item.get("value")
                    or license_item
                ),
                200,
            )
            for license_item in license_values[:10]
            if license_item
        ]
        normalized["locations"] = [
            truncate_text(str(location.get("path") or ""), 500)
            for location in location_values[:10]
            if isinstance(location, dict) and location.get("path")
        ]
        return normalized

    def _normalize_cyclonedx_package(self, item: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._base_package(item)
        license_values = item.get("licenses")
        license_values = license_values if isinstance(license_values, list) else []
        licenses: List[str] = []
        for entry in license_values[:10]:
            if not isinstance(entry, dict):
                continue
            license_data = entry.get("license")
            if isinstance(license_data, dict):
                value = license_data.get("id") or license_data.get("name")
            else:
                value = entry.get("expression")
            if value:
                licenses.append(truncate_text(str(value), 200))
        normalized["licenses"] = licenses
        normalized["locations"] = []
        return normalized

    def _normalize_spdx_package(self, item: Dict[str, Any]) -> Dict[str, Any]:
        purl: Optional[str] = None
        external_refs = item.get("externalRefs")
        if isinstance(external_refs, list):
            for external_ref in external_refs[:20]:
                if not isinstance(external_ref, dict):
                    continue
                if str(external_ref.get("referenceType") or "").lower() == "purl":
                    purl = str(external_ref.get("referenceLocator") or "") or None
                    break
        package_type = str(item.get("primaryPackagePurpose") or "").lower()
        if not package_type and purl:
            match = re.match(r"^pkg:([^/]+)/", purl, flags=re.IGNORECASE)
            if match:
                purl_type = match.group(1).lower()
                package_type = {"pypi": "python"}.get(purl_type, purl_type)
        return {
            "name": truncate_text(str(item.get("name") or "unknown"), 300),
            "version": truncate_text(str(item.get("versionInfo") or ""), 200),
            "type": truncate_text(package_type or "unknown", 100),
            "purl": truncate_text(purl, 500) if purl else None,
            "licenses": string_list(
                item.get("licenseConcluded") or item.get("licenseDeclared"),
                limit=10,
                max_chars=200,
            ),
            "locations": [],
        }

    @staticmethod
    def _base_package(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": truncate_text(str(item.get("name") or "unknown"), 300),
            "version": truncate_text(str(item.get("version") or ""), 200),
            "type": truncate_text(str(item.get("type") or "unknown"), 100),
            "purl": (
                truncate_text(str(item.get("purl")), 500) if item.get("purl") else None
            ),
        }

    @staticmethod
    def _engine_version(report: Dict[str, Any], output_format: SbomFormat) -> str:
        if output_format == SbomFormat.SYFT_JSON:
            descriptor = report.get("descriptor")
            if isinstance(descriptor, dict) and descriptor.get("version"):
                return truncate_text(str(descriptor["version"]), 100)
        if output_format == SbomFormat.CYCLONEDX_JSON:
            metadata = report.get("metadata")
            tools = metadata.get("tools") if isinstance(metadata, dict) else None
            components = tools.get("components") if isinstance(tools, dict) else None
            if isinstance(components, list):
                for component in components:
                    if (
                        isinstance(component, dict)
                        and str(component.get("name") or "").lower() == "syft"
                        and component.get("version")
                    ):
                        return truncate_text(str(component["version"]), 100)
        if output_format == SbomFormat.SPDX_JSON:
            creation_info = report.get("creationInfo")
            creators = (
                creation_info.get("creators")
                if isinstance(creation_info, dict)
                else None
            )
            if isinstance(creators, list):
                for creator in creators:
                    match = re.fullmatch(
                        r"Tool:\s*syft-(.+)", str(creator).strip(), flags=re.IGNORECASE
                    )
                    if match:
                        return truncate_text(match.group(1), 100)
        return "unknown"

    def _command_failure(
        self,
        result: AnchoreCommandResult,
        source: ResolvedSupplyChainSource,
        report_path: Path,
        log_path: Path,
        runtime: AnchoreRuntimeInfo,
    ) -> ToolResult:
        reason = result.error or f"Syft exited with code {result.exit_code}"
        return self._failure(reason, source, report_path, log_path, runtime=runtime)

    def _failure(
        self,
        reason: str,
        source: ResolvedSupplyChainSource,
        report_path: Path,
        log_path: Path,
        runtime: Optional[AnchoreRuntimeInfo],
    ) -> ToolResult:
        safe_reason = truncate_text(reason, 1200)
        payload: Dict[str, Any] = {
            "status": "failed",
            "engine": "syft",
            "source": source.display,
            "reason": safe_reason,
            "diagnostic_tail": tail_log(log_path),
            "artifacts": {"report": str(report_path), "log": str(log_path)},
        }
        if runtime is not None:
            payload["runtime"] = runtime_payload(runtime)
        return ToolResult(
            success=False,
            output=json.dumps(payload, indent=2, sort_keys=True),
            error=f"Syft SBOM generation failed: {safe_reason}",
            metadata={
                "status": "failed",
                "engine": "syft",
                "report_artifact": str(report_path),
                "log_artifact": str(log_path),
                "runtime_source": runtime.source if runtime is not None else None,
            },
        )
