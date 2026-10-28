"""Optional Ansible execution and immutable engagement automation profiles."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable, Literal, Type

from pydantic import ConfigDict, Field, ValidationError

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.automation.ansible import (
    AnsibleExecutor,
    ansible_binary,
    load_summary,
    run_directory,
)
from kerkov.core.automation.profiles import (
    ProfileStore,
    file_digest,
    local_file,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import RESOURCE_SECTIONS, LedgerEntry
from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class AnsibleParams(ToolParams):
    model_config = ConfigDict(extra="forbid")

    action: Literal["run", "snapshot", "profile"] = Field(
        default="run",
        description="Run a playbook, snapshot a profile template, or read a saved engagement profile.",
    )
    playbook: str | None = Field(
        default=None,
        max_length=4096,
        description="Local playbook file, or a declared playbook name when using profile.",
    )
    inventory: str | None = Field(
        default=None,
        max_length=4096,
        description="Explicit local static YAML/JSON/INI inventory for a run without profile. Dynamic inventory plugins/scripts are unsupported.",
    )
    profile: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
        description="Saved engagement profile name for profile/run actions.",
    )
    template: str | None = Field(
        default=None,
        max_length=4096,
        description="Local YAML/JSON profile template for snapshot. Host bindings reference existing ledger resource IDs.",
    )
    limit: str = Field(
        default="all",
        min_length=1,
        max_length=512,
        description="Ansible host/group selection pattern. This is not a permission boundary.",
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Optional task tags to include.",
    )
    skip_tags: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Optional task tags to exclude.",
    )
    vars_files: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Local variable file references. Do not send passwords or key contents in tool arguments.",
    )
    check: bool = Field(
        default=True,
        description="Request Ansible check mode. Tasks can override it; this does not guarantee read-only execution.",
    )
    diff: bool = Field(
        default=False,
        description="Include supported configuration diffs in the private execution log; diffs may contain sensitive data.",
    )
    forks: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum host concurrency, additionally capped by a selected profile.",
    )
    timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="Maximum playbook execution runtime in seconds.",
    )
    max_hosts: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Maximum per-host recaps returned. Full recap and logs remain in the workspace.",
    )


class AnsibleTool(BaseTool):
    """Run trusted local automation with shared approval and artifact handling."""

    def __init__(
        self,
        *,
        execution_enabled: bool = False,
        ledger_getter: Callable[[], FindingsLedger | None] | None = None,
        executor: AnsibleExecutor | None = None,
        binary: str | None = None,
    ) -> None:
        super().__init__(
            name="ansible",
            description=(
                "Run reviewed local Ansible playbooks against an explicit static inventory "
                "or an engagement profile. Supports multiple hosts, limit, tags, check/diff, "
                "and bounded per-host recaps. Playbooks/inventory/vars are trusted executable "
                "input and may act on the controller or delegate beyond inventory. Check "
                "mode is not guaranteed read-only. snapshot freezes a template's host "
                "bindings and playbook hashes in the active root engagement workspace; "
                "profile reads it. Profiles reference existing ledger assets/targets and "
                "do not grant execution permission. Execution requires a POSIX controller "
                "(Linux/macOS or WSL) and ansible-playbook installed "
                "outside the tool; no automatic installation, credentials in arguments, "
                "or arbitrary CLI flags. Not a compliance scanner."
            ),
        )
        self.execution_enabled = execution_enabled
        self._ledger_getter = ledger_getter
        self._executor = executor or AnsibleExecutor()
        self._binary = binary

    def get_params_class(self) -> Type[ToolParams]:
        return AnsibleParams

    def approval_scope(self, params: dict[str, Any]) -> ApprovalScope:
        # Paths select mutable executable content, so a saved call is not a fixed grant.
        return ApprovalScope(
            saveable=False, volatile_params=frozenset({"timeout_seconds"})
        )

    def validate_params(self, toolParams: ToolParams) -> str | None:
        if not isinstance(toolParams, AnsibleParams):
            return "invalid Ansible parameters"
        params = toolParams
        if params.action == "snapshot":
            if (
                not params.template
                or params.profile
                or params.inventory
                or params.playbook
            ):
                return "snapshot requires template only; the template declares its profile name"
        elif params.action == "profile":
            if (
                not params.profile
                or params.template
                or params.inventory
                or params.playbook
            ):
                return "profile requires a saved profile name only"
        elif (
            not params.playbook
            or params.template
            or bool(params.inventory) == bool(params.profile)
        ):
            return "run requires playbook and exactly one of inventory or profile"
        for value in (
            params.playbook,
            params.inventory,
            params.template,
            params.limit,
            *params.vars_files,
        ):
            if value is not None and (
                not value.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                return (
                    "paths and limit must be nonempty and contain no control characters"
                )
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", tag)
            for tag in (*params.tags, *params.skip_tags)
        ):
            return "tags must contain ordinary tag names without commas or spaces"
        return None

    async def _rows(self) -> list[LedgerEntry]:
        ledger = self._ledger_getter() if self._ledger_getter else None
        if ledger is None:
            raise ValueError(
                "engagement profiles require the active findings ledger (KB_ENABLED)"
            )
        return await ledger.list(sections=RESOURCE_SECTIONS)

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, AnsibleParams):
            raise TypeError("expected AnsibleParams")
        params = toolParams
        if not self.execution_enabled:
            return ToolResult(
                success=False,
                output="",
                error="Ansible is disabled. Set EXECUTION_ENABLED=true.",
                metadata={"policy": "execution_disabled"},
            )
        metadata: dict[str, Any] = {"action": params.action, "status": "failed"}
        try:
            workspace = get_current_workspace()
            if workspace is None:
                raise ValueError("Ansible requires an active engagement workspace")
            workspace = workspace.resolve()
            store = ProfileStore(workspace)
            if params.action == "snapshot":
                path = local_file(params.template or "", workspace)
                rows = await self._rows()
                snapshot = await asyncio.to_thread(store.create, path, rows)
                name = snapshot["profile"]["name"]
                return ToolResult(
                    success=True,
                    output=json.dumps(snapshot, indent=2),
                    metadata={
                        "action": params.action,
                        "status": "snapshotted",
                        "profile": name,
                        "profile_artifact": str(store.path(name)),
                    },
                )
            if params.action == "profile":
                snapshot = await asyncio.to_thread(store.load, params.profile or "")
                return ToolResult(
                    success=True,
                    output=json.dumps(snapshot, indent=2),
                    metadata={
                        "action": params.action,
                        "status": "loaded",
                        "profile": params.profile,
                    },
                )
            binary = self._binary or ansible_binary()
            if not binary:
                raise ValueError(
                    "ansible-playbook is unavailable. Run `kerkov setup ansible` for setup guidance; use Linux/macOS or WSL as the controller."
                )
            if params.profile:
                rows = await self._rows()
                inventory_data, playbook, max_forks = await asyncio.to_thread(
                    store.resolve, params.profile, params.playbook or "", rows
                )
                inventory = None
            else:
                inventory_data = None
                playbook = local_file(params.playbook or "", workspace)
                inventory = local_file(params.inventory or "", workspace)
                if inventory.suffix.lower() not in {".yaml", ".yml", ".json", ".ini"}:
                    raise ValueError("inventory must be a static YAML/JSON/INI file")
                max_forks = 20
            variables = [local_file(path, workspace) for path in params.vars_files]
            digest = await asyncio.to_thread(file_digest, playbook)
            directory = await asyncio.to_thread(run_directory, workspace)
            if inventory_data is not None:
                inventory = directory / "inventory.json"
                await asyncio.to_thread(
                    inventory.write_text, json.dumps(inventory_data, sort_keys=True)
                )
            if inventory is None:
                raise ValueError(
                    "an explicit or profile-generated inventory is required"
                )
            metadata.update(
                {
                    "playbook": str(playbook),
                    "playbook_sha256": digest,
                    "inventory": str(inventory),
                    "profile": params.profile,
                    "check": params.check,
                    "diff": params.diff,
                    "forks": min(params.forks, max_forks),
                    "log_artifact": str(directory / "ansible.log"),
                    "summary_artifact": str(directory / "summary.json"),
                }
            )
            args = [
                binary,
                "--inventory",
                str(inventory),
                f"--limit={params.limit}",
                "--forks",
                str(min(params.forks, max_forks)),
            ]
            if params.check:
                args.append("--check")
            if params.diff:
                args.append("--diff")
            if params.tags:
                args.append("--tags=" + ",".join(params.tags))
            if params.skip_tags:
                args.append("--skip-tags=" + ",".join(params.skip_tags))
            for path in variables:
                args.extend(["--extra-vars", "@" + str(path)])
            args.append(str(playbook))
            command = await self._executor.run(args, directory, params.timeout_seconds)
            metadata.update(
                exit_code=command.exit_code,
                timed_out=command.timed_out,
                command_error=command.error,
            )
            if command.error or command.timed_out:
                raise ValueError(
                    f"Ansible did not finish: {command.error or 'execution timed out'}; "
                    f"inspect {directory / 'ansible.log'}"
                )
            try:
                summary = await asyncio.to_thread(
                    load_summary, directory, params.max_hosts
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    f"Ansible produced no recap (exit {command.exit_code}); "
                    f"inspect {directory / 'ansible.log'}"
                ) from exc
            success = (
                command.exit_code == 0
                and not summary["totals"]["failures"]
                and not summary["totals"]["unreachable"]
            )
            metadata.update(host_count=summary["host_count"], totals=summary["totals"])
            metadata["status"] = "completed" if success else "failed"
            return ToolResult(
                success=success,
                output=json.dumps({**summary, **metadata}, separators=(",", ":")),
                error=(
                    None
                    if success
                    else "Ansible reported failed or unreachable hosts; inspect the recap and saved log"
                ),
                metadata=metadata,
            )
        except ValidationError as exc:
            details = "; ".join(
                ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
                for item in exc.errors(include_input=False, include_context=False)[:10]
            )
            return ToolResult(
                success=False,
                output=json.dumps(metadata),
                error="Invalid automation profile: " + details,
                metadata=metadata,
            )
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            return ToolResult(
                success=False,
                output=json.dumps(metadata, sort_keys=True),
                error=str(exc),
                metadata=metadata,
            )
