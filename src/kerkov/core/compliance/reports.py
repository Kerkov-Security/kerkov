"""Preserve original assessment evidence beside bounded, normalized summaries."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kerkov.core.automation.profiles import confined_path, local_file
from kerkov.core.compliance import cinc_report

MAX_REPORT_BYTES = 64 * 1024 * 1024


class ReportImportError(ValueError):
    """Rejected evidence with a preserved source and its provenance."""

    def __init__(self, message: str, metadata: dict[str, Any]) -> None:
        super().__init__(message)
        self.metadata = metadata


def new_directory(workspace: Path, prefix: str) -> Path:
    root = confined_path(workspace, "artifacts/compliance")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def import_report(
    workspace: Path,
    report: str,
    *,
    expected_profile: str | None,
    max_controls: int,
    directory: Path | None = None,
) -> dict[str, Any]:
    source = local_file(report, workspace)
    with source.open("rb") as handle:
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Compliance report exceeds the 64 MiB processing limit")
    directory = directory or new_directory(workspace, "report-")
    evidence = source if source.is_relative_to(directory) else directory / "source.json"
    normalized = directory / "normalized.json"
    if evidence != source:
        evidence.write_bytes(raw)
    provenance = {
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(source),
        "report_artifact": str(evidence),
        "normalized_artifact": str(normalized),
    }
    try:
        parsed = cinc_report.parse_report(raw, expected_profile=expected_profile)
    except ValueError as exc:
        raise ReportImportError(
            str(exc),
            {
                key: value
                for key, value in provenance.items()
                if key != "normalized_artifact"
            },
        ) from exc
    normalized.write_text(
        json.dumps({**parsed, **provenance}, indent=2), encoding="utf-8"
    )
    return {
        **parsed,
        **provenance,
        "controls": parsed["controls"][:max_controls],
        "controls_truncated": len(parsed["controls"]) > max_controls,
        "evidence_status": "imported",
        "notice": "Scanner observations, not an independent attestation. Skipped, waived, errored and empty checks are not passes. Verify host coverage and identity before recording findings; no ledger entries were changed.",
    }
