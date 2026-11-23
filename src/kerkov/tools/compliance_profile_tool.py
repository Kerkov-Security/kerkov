"""Prepare upstream profile projects through shared tool execution and approval."""

from __future__ import annotations

import asyncio
import json
import sys
import tarfile
from pathlib import Path
from typing import Any, Literal, Type

from pydantic import ConfigDict, Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.compliance.profile_sources import (
    COMMIT_PATTERN,
    ProfileSource,
    profile_catalog,
)
from kerkov.core.compliance.projects import ProfileProjects
from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ComplianceProfileParams(ToolParams):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "validate", "prepare"] = Field(
        description="List catalog/cache entries, validate a public GitHub profile URL, or download and prepare a local project for compliance_scan."
    )
    url: str | None = Field(
        default=None,
        max_length=2048,
        description="Public https://github.com/owner/repository URL, optionally /tree/branch-or-tag. Required for validate/prepare.",
    )
    expected_commit: str | None = Field(
        default=None,
        description="Commit returned by validate after reviewing the dependency list. Required when downloading or refreshing a project.",
    )
    refresh: bool = Field(
        default=False,
        description="Explicitly fetch and prepare the current upstream source. Otherwise reuse the existing ready copy without network requests.",
    )
    timeout_seconds: int = Field(
        default=600,
        ge=30,
        le=1800,
        description="Preparation deadline including dependency downloads and offline Cinc profile validation.",
    )


class ComplianceProfileTool(BaseTool):
    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        projects: ProfileProjects | None = None,
    ) -> None:
        super().__init__(
            name="compliance_profile",
            description="Use upstream InSpec projects without adding their checks to Kerkov. List available catalog/downloaded projects, validate public GitHub URLs, and prepare cached profiles. Validate before downloading or refreshing and review the returned dependency sources. Preparation downloads source and may execute trusted Ruby during offline Cinc validation; it never assesses or remediates a target. Normal approvals apply. Prepare reuses a ready copy until refresh=true. Failed refresh preserves the previous copy. Use the returned profile path with compliance_scan.",
        )
        self.config = config or get_config()
        self.projects = projects or ProfileProjects(
            Path(self.config.compliance_projects_dir),
            image=self.config.cinc_docker_image,
        )

    def get_params_class(self) -> Type[ToolParams]:
        return ComplianceProfileParams

    def validate_params(self, toolParams: ToolParams) -> str | None:
        if not isinstance(toolParams, ComplianceProfileParams):
            return "Invalid compliance profile parameters"
        if toolParams.action != "list":
            try:
                ProfileSource.parse(toolParams.url)
            except ValueError as exc:
                return str(exc)
        if toolParams.expected_commit is not None:
            if toolParams.action != "prepare":
                return "expected_commit is only available for prepare"
            if not COMMIT_PATTERN.fullmatch(toolParams.expected_commit):
                return "expected_commit must be a full Git commit"
        if toolParams.refresh and toolParams.action != "prepare":
            return "refresh is only available for prepare"
        return None

    def approval_scope(self, params: dict[str, Any]) -> ApprovalScope:
        if params.get("action") in {"list", "validate"}:
            return ApprovalScope.keeping(
                params, ("action",), volatile=("timeout_seconds",)
            )
        return ApprovalScope(
            saveable=False, volatile_params=frozenset({"timeout_seconds"})
        )

    async def run(self, toolParams: ToolParams) -> ToolResult:
        params = ComplianceProfileParams.model_validate(toolParams.model_dump())
        try:
            if params.action == "list":
                result = {
                    "catalog": profile_catalog(),
                    "projects": await asyncio.to_thread(self.projects.list),
                }
            elif params.action == "validate":
                result = await self.projects.remote.validate(params.url or "")
            else:
                if not self.config.execution_enabled:
                    raise ValueError(
                        "Enable EXECUTION_ENABLED before preparing executable profile projects"
                    )
                if sys.platform == "win32":
                    raise ValueError(
                        "Use Linux, macOS or WSL to prepare profile projects"
                    )
                workspace = get_current_workspace()
                if workspace is None:
                    raise ValueError(
                        "Open an engagement before preparing a profile project"
                    )
                result = await self.projects.prepare(
                    params.url or "",
                    workspace.resolve(),
                    refresh=params.refresh,
                    expected_commit=params.expected_commit,
                    timeout_seconds=params.timeout_seconds,
                )
            return ToolResult(
                success=True, output=json.dumps(result, indent=2), metadata=result
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            tarfile.TarError,
            EOFError,
            TimeoutError,
        ) as exc:
            return ToolResult(success=False, output="", error=str(exc))
