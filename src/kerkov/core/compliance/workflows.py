"""Durable single-host compliance plans and assessment comparisons."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kerkov.core.automation.profiles import confined_path
from kerkov.core.private_files import prepare_private_parent
from kerkov.tools.ansible_tool import AnsibleParams
from kerkov.tools.compliance_scan_tool import ComplianceScanParams


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["assess", "preview", "apply"]
    resource_id: int = Field(gt=0, strict=True)
    baseline_id: str | None = Field(default=None, max_length=64)
    scan: ComplianceScanParams
    remediation: AnsibleParams | None = None
    preview_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    assessment_of: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    def signature(self) -> str:
        data = self.model_dump(exclude={"action", "preview_id", "assessment_of"})
        data["scan"].pop("timeout_seconds", None)
        data["scan"].pop("max_controls", None)
        if data["remediation"]:
            data["remediation"].pop("check", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class WorkflowStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = confined_path(self.workspace, "automation/compliance-runs")

    def path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid compliance run ID")
        path = self.root / f"{run_id}.json"
        if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
            raise ValueError("Compliance records must stay in the engagement workspace")
        return path

    def save(self, record: dict[str, Any]) -> None:
        destination = self.path(record["id"])
        prepare_private_parent(self.root, managed_parent=self.root)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.root, delete=False
            ) as f:
                temporary = Path(f.name)
                json.dump(record, f, ensure_ascii=True)
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, run_id: str) -> dict[str, Any]:
        with self.path(run_id).open("rb") as f:
            raw = f.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Compliance record exceeds processing limit")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("id") != run_id:
            raise ValueError("Invalid compliance record")
        WorkflowRequest.model_validate(record.get("request"))
        if record.get("version") != 1 or any(
            not isinstance(record.get(key), str)
            for key in (
                "label",
                "created_at",
                "updated_at",
                "stage",
                "status",
                "detail",
            )
        ):
            raise ValueError("Invalid compliance record metadata")
        for phase in ("before", "after"):
            report = record.get(phase)
            if report is not None and not isinstance(report, dict):
                raise ValueError("Invalid compliance evidence")
        return record

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        if not self.root.resolve().is_relative_to(self.workspace):
            raise ValueError("Compliance records must stay in the engagement workspace")
        paths = sorted(
            self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        records = []
        for path in paths[:30]:
            try:
                records.append(self.load(path.stem))
            except (OSError, ValueError):
                continue
        return records


def new_record(request: WorkflowRequest, label: str) -> dict[str, Any]:
    return {
        "version": 1,
        "id": uuid.uuid4().hex,
        "request": request.model_dump(),
        "signature": request.signature(),
        "label": label,
        "created_at": now(),
        "updated_at": now(),
        "stage": "readiness",
        "status": "running",
        "detail": "Checking controller prerequisites. SSH authentication and target compatibility are checked during approved execution.",
        "readiness": [],
        "before": None,
        "after": None,
        "remediation": None,
        "plan": {},
        "events": [],
    }


def control_comparison(record: dict[str, Any]) -> list[dict[str, Any]]:
    before = {
        (c["profile"], c["control_id"]): c
        for c in (record.get("before") or {}).get("controls", [])
    }
    after = {
        (c["profile"], c["control_id"]): c
        for c in (record.get("after") or {}).get("controls", [])
    }
    return [
        {
            **after.get(key, before.get(key, {})),
            "before": before.get(key, {}).get("status", "unassessed"),
            "after": after.get(key, {}).get("status", "unassessed"),
        }
        for key in dict.fromkeys([*before, *after])
    ]
