"""Run an approved OpenSCAP assessment through the shared Ansible executor."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kerkov.core.automation.ansible import bundled_playbook, ssh_inventory_host
from kerkov.core.automation.profiles import local_file, read_document
from kerkov.core.compliance.openscap import (
    MAX_PROFILE_ID_CHARS,
    PROFILE_ID_PATTERN,
    assessment_controls,
    datastream_bytes,
    import_report,
)
from kerkov.core.compliance.reports import new_directory

if TYPE_CHECKING:
    from kerkov.tools.base_tool import ToolResult
    from kerkov.tools.compliance_scan_tool import ComplianceScanParams


class OpenScapSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distribution: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,63}$",
        description="Exact target Ansible distribution fact, such as Ubuntu or RedHat.",
    )
    version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="Exact target Ansible distribution_version fact, such as 24.04.",
    )
    profile_id: str = Field(
        pattern=PROFILE_ID_PATTERN,
        max_length=MAX_PROFILE_ID_CHARS,
        description="Full XCCDF profile ID in the reviewed datastream.",
    )
    sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="SHA-256 of the reviewed datastream."
    )


_HOST_ALIAS = "selected_asset"


def _prepare(
    workspace: Path,
    params: ComplianceScanParams,
    settings: OpenScapSettings,
    directory: Path,
) -> dict[str, Any]:
    key = local_file(params.private_key_file, workspace)
    known_hosts = local_file(params.known_hosts_file, workspace)
    stat = key.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError(
            "SSH private key must be owned by you with owner-only permissions"
        )
    source = local_file(params.profile, workspace)
    raw = datastream_bytes(source, settings.sha256, settings.profile_id)
    datastream = directory / "datastream.xml"
    datastream.write_bytes(raw)
    host = ssh_inventory_host(
        params.host, params.user, params.port, str(key), str(known_hosts)
    )
    host["ansible_become"] = params.sudo and params.user != "root"
    inventory = directory / "inventory.json"
    inventory.write_text(
        json.dumps({"all": {"hosts": {_HOST_ALIAS: host}}}), encoding="utf-8"
    )
    variables = directory / "vars.json"
    variables.write_text(
        json.dumps(
            {
                "openscap_expected_distribution": settings.distribution,
                "openscap_expected_version": settings.version,
                "openscap_content": str(datastream),
                "openscap_content_sha256": settings.sha256,
                "openscap_profile": settings.profile_id,
                "openscap_controller_content": True,
            }
        ),
        encoding="utf-8",
    )
    return {
        "playbook": str(bundled_playbook("openscap_assess")),
        "inventory": str(inventory),
        "vars_files": [str(variables)],
        "limit": _HOST_ALIAS,
        "check": False,
        "timeout_seconds": params.timeout_seconds,
        "max_hosts": 1,
    }


def _collect(
    workspace: Path,
    params: ComplianceScanParams,
    settings: OpenScapSettings,
    artifacts: Path,
) -> dict[str, Any]:
    directory = artifacts / "openscap" / _HOST_ALIAS
    manifest_path = directory / "manifest.json"
    manifest = read_document(manifest_path)
    if (
        not isinstance(manifest, dict)
        or any(
            manifest.get(key) != expected
            for key, expected in {
                "inventory_alias": _HOST_ALIAS,
                "inventory_address": params.host,
                "distribution": settings.distribution,
                "distribution_version": settings.version,
                "content_sha256": settings.sha256,
                "profile_id": settings.profile_id,
            }.items()
        )
        or manifest.get("scanner_exit_code") not in {0, 2}
    ):
        raise ValueError("OpenSCAP manifest does not match the approved assessment")
    report = import_report(
        workspace,
        str(directory / "results-arf.xml"),
        expected_profile=settings.profile_id,
        result_id=None,
        max_rules=None,
        evidence_status="scanned",
    )
    summary, _ = assessment_controls(report)
    return {
        **{
            k: v
            for k, v in report.items()
            if k not in {"counts", "rules", "rules_truncated"}
        },
        **summary,
        "scanner_version": str(manifest.get("scanner_version", "")).split("\n", 1)[0][
            :200
        ],
        "scanner_binary": str(manifest.get("scanner_binary", "/usr/bin/oscap")),
        "manifest_artifact": str(manifest_path),
        "html_artifact": str(directory / "report.html"),
        "arf_artifact": str(directory / "results-arf.xml"),
        "exit_code": manifest["scanner_exit_code"],
    }


async def scan_openscap(
    workspace: Path,
    params: ComplianceScanParams,
    settings: OpenScapSettings,
    *,
    execution_enabled: bool,
) -> ToolResult:
    # ToolSuite imports OpenScapSettings while registering ComplianceScanTool.
    from kerkov.tools.ansible_tool import AnsibleTool
    from kerkov.tools.base_tool import ToolResult

    if not execution_enabled:
        return ToolResult(
            success=False,
            output="",
            error="Compliance scanning is disabled. Set EXECUTION_ENABLED=true.",
            metadata={"policy": "execution_disabled"},
        )
    metadata = {
        "engine": "openscap",
        "status": "failed",
        "target": {"host": params.host, "user": params.user, "port": params.port},
        "sudo": params.sudo,
        "profile_name": settings.profile_id,
        "profile_sha256": settings.sha256,
        "profile_path": params.profile,
        "platform": {
            "distribution": settings.distribution,
            "version": settings.version,
        },
        "run_started_at": datetime.now(timezone.utc).isoformat(),
    }
    directory: Path | None = None
    try:
        if os.name == "nt":
            raise ValueError(
                "OpenSCAP assessment requires Ansible on Linux, macOS or WSL"
            )
        directory = new_directory(workspace, "openscap-")
        metadata["run_manifest_artifact"] = str(directory / "manifest.json")
        arguments = await asyncio.to_thread(
            _prepare, workspace, params, settings, directory
        )
        result = await AnsibleTool(execution_enabled=execution_enabled).execute(
            arguments
        )
        ansible_metadata = result.metadata or {}
        for key in ("artifact_directory", "log_artifact", "summary_artifact"):
            if key in ansible_metadata:
                metadata[key] = ansible_metadata[key]
        if not result.success:
            if any(
                task["task"].endswith("Require a working scanner before assessing")
                for task in json.loads(result.output or "{}").get("failed_tasks", [])
            ):
                raise ValueError(
                    "OpenSCAP is missing on the target. Ask the operator whether to provision it with builtin:openscap_install."
                )
            raise ValueError(result.error or "OpenSCAP failed; inspect the Ansible log")
        summary = await asyncio.to_thread(
            _collect,
            workspace,
            params,
            settings,
            Path(ansible_metadata["artifact_directory"]),
        )
        controls = summary.pop("controls")
        metadata.update({k: v for k, v in summary.items() if k != "notice"})
        metadata["status"] = "completed"
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    **summary,
                    **metadata,
                    "controls": controls[: params.max_controls],
                    "controls_truncated": len(controls) > params.max_controls,
                },
                indent=2,
            ),
            metadata=metadata,
        )
    except asyncio.CancelledError:
        metadata["status"] = "cancelled"
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        metadata["error"] = str(exc)
        return ToolResult(
            success=False,
            output=json.dumps(metadata),
            error=str(exc),
            metadata=metadata,
        )
    except Exception as exc:
        metadata["status"] = "internal_error"
        metadata["error"] = str(exc)
        raise
    finally:
        if directory is not None:
            metadata["run_finished_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                (directory / "manifest.json").write_text,
                json.dumps(metadata, indent=2),
                encoding="utf-8",
            )
