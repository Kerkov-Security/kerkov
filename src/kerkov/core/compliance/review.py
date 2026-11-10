"""Build local control reviews and portable reports from saved assessment evidence."""

from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path
from typing import Any

from kerkov.core.compliance.cinc_report import (
    MAX_REPORT_BYTES,
    assertion_status,
    decode_report,
    summarize_report,
)
from kerkov.core.compliance.workflows import control_comparison

MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_CONTROL_TESTS = 20
MAX_DETAIL_CHARS = 2000
NOTICE = (
    "These are selected baseline observations, not overall compliance certification. "
    "Skipped, waived, errored and empty checks are not passes. "
    "Remediation success alone does not verify a control. "
    "Assertion messages can contain sensitive target configuration."
)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value[:MAX_DETAIL_CHARS] + (
        "\n[Text truncated]" if len(value) > MAX_DETAIL_CHARS else ""
    )


_ReportSource = tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]


class ReviewEvidence:
    """Reuse parsed evidence for two assessment phases after checking each digest."""

    def __init__(self) -> None:
        self._reports: dict[tuple[Path, str, str | None], _ReportSource] = {}

    def load(self, workspace: Path, assessment: dict[str, Any]) -> _ReportSource:
        path, raw, digest = _read_source(workspace, assessment)
        expected_profile = assessment.get("profile_name")
        key = (path, digest, expected_profile)
        cached = self._reports.get(key)
        if cached is not None:
            return cached
        if len(self._reports) == 2:
            del self._reports[next(iter(self._reports))]
        result = _parse_source(raw, expected_profile)
        self._reports[key] = result
        return result


def _read_source(
    workspace: Path, assessment: dict[str, Any]
) -> tuple[Path, bytes, str]:
    reference = assessment.get("report_artifact")
    if not isinstance(reference, str) or not reference:
        raise ValueError("Original assessment evidence is unavailable")
    path = Path(reference).resolve()
    root = (workspace / "artifacts/compliance").resolve()
    if not root.is_relative_to(workspace.resolve()) or not path.is_relative_to(root):
        raise ValueError("Evidence must belong to this engagement workspace")
    if not path.is_file():
        raise ValueError("Original assessment evidence is unavailable")
    with path.open("rb") as handle:
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Assessment evidence exceeds the 64 MiB processing limit")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != assessment.get("report_sha256"):
        raise ValueError("Assessment evidence no longer matches its saved SHA-256")
    return path, raw, digest


def _parse_source(raw: bytes, expected_profile: str | None) -> _ReportSource:
    source = decode_report(raw)
    parsed = summarize_report(source, expected_profile=expected_profile)
    entries = {}
    for profile in source["profiles"]:
        for control in profile.get("controls", []):
            key = (profile["name"], control["id"])
            if key in entries:
                raise ValueError("Assessment contains ambiguous duplicate control IDs")
            entries[key] = control
    return parsed, entries


def _assertions(control: dict[str, Any]) -> dict[str, Any]:
    results = control.get("results", [])
    return {
        "description": _text(control.get("desc")),
        "tests": [
            {
                "status": assertion_status(result),
                **{
                    key: _text(result.get(key))
                    for key in ("code_desc", "message", "skip_message", "exception")
                },
            }
            for result in results[:MAX_CONTROL_TESTS]
        ],
        "test_count": len(results),
        "tests_truncated": len(results) > MAX_CONTROL_TESTS,
    }


def _remediation(record: dict[str, Any], profile: str, control_id: str) -> str:
    baseline = record.get("plan", {}).get("baseline") or {}
    if profile == baseline.get("profile_name"):
        fix = (baseline.get("remediation") or {}).get(control_id)
        if isinstance(fix, str) and fix:
            return fix
    return "Manual review required: no saved control-to-fix mapping is available."


def review_control(
    workspace: Path,
    record: dict[str, Any],
    profile: str,
    control_id: str,
    *,
    evidence: ReviewEvidence | None = None,
) -> dict[str, Any]:
    evidence = evidence or ReviewEvidence()
    phases = {}
    for phase in ("before", "after"):
        assessment = record.get(phase)
        if not assessment:
            phases[phase] = {"status": "unassessed", "tests": []}
            continue
        try:
            parsed, entries = evidence.load(workspace, assessment)
            control = next(
                (
                    item
                    for item in parsed["controls"]
                    if (item["profile"], item["control_id"]) == (profile, control_id)
                ),
                None,
            )
            phases[phase] = (
                {
                    **control,
                    **_assertions(entries[(profile, control_id)]),
                    "report_artifact": assessment["report_artifact"],
                    "report_sha256": assessment["report_sha256"],
                }
                if control
                else {"status": "unassessed", "tests": []}
            )
        except (OSError, ValueError) as exc:
            phases[phase] = {
                "status": "unavailable",
                "tests": [],
                "detail": str(exc),
            }
    return {
        "profile": profile,
        "control_id": control_id,
        "remediation": _remediation(record, profile, control_id),
        "notice": NOTICE,
        **phases,
    }


def build_report(
    workspace: Path, record: dict[str, Any], *, evidence: ReviewEvidence | None = None
) -> dict[str, Any]:
    evidence = evidence or ReviewEvidence()
    assessments: dict[str, Any] = {}
    full = {}
    entries = {}
    for phase in ("before", "after"):
        saved = record.get(phase)
        if not saved:
            assessments[phase] = None
            continue
        parsed, entries[phase] = evidence.load(workspace, saved)
        full[phase] = parsed
        assessments[phase] = {
            key: value for key, value in parsed.items() if key != "controls"
        }
        assessments[phase].update(
            {
                key: saved.get(key)
                for key in (
                    "status",
                    "evidence_status",
                    "target",
                    "sudo",
                    "profile_sha256",
                    "image",
                    "image_id",
                    "exit_code",
                    "cleanup_error",
                    "imported_at",
                    "report_sha256",
                    "report_artifact",
                    "normalized_artifact",
                    "manifest_artifact",
                )
            }
        )
    controls = control_comparison(full)
    for control in controls:
        key = (control["profile"], control["control_id"])
        control["remediation"] = _remediation(record, *key)
        for phase in ("before", "after"):
            source = entries.get(phase, {}).get(key)
            control[f"{phase}_evidence"] = _assertions(source) if source else None
    request = record["request"]
    plan = record.get("plan", {})
    remediation = record.get("remediation")
    baseline = plan.get("baseline")
    status = record["status"]
    if status == "running":
        status = "incomplete"
    return {
        "schema_version": 1,
        "notice": NOTICE,
        "run": {
            **{
                key: record.get(key)
                for key in (
                    "id",
                    "label",
                    "stage",
                    "detail",
                    "created_at",
                    "updated_at",
                )
            },
            "status": status,
            "detail": (
                "No final workflow outcome was recorded. Review evidence before retrying changes."
                if record["status"] == "running"
                else record["detail"]
            ),
            "action": request["action"],
            "preview_id": request.get("preview_id"),
            "assessment_of": request.get("assessment_of"),
        },
        "asset": {
            "resource_id": request["resource_id"],
            **{
                key: request["scan"].get(key)
                for key in ("host", "port", "user", "sudo")
            },
        },
        "baseline": (
            {
                key: baseline.get(key)
                for key in ("id", "name", "version", "platform", "profile_name")
            }
            if baseline
            else None
        ),
        "plan": {key: plan.get(key) for key in ("fingerprint", "coverage", "changes")},
        "assessments": assessments,
        "remediation": (
            {
                key: remediation.get(key)
                for key in (
                    "success",
                    "check",
                    "status",
                    "exit_code",
                    "timed_out",
                    "log_artifact",
                    "totals",
                    "playbook_sha256",
                )
            }
            if remediation is not None
            else None
        ),
        "controls": controls,
    }


def _evidence_html(evidence: dict[str, Any] | None) -> str:
    if evidence is None:
        return "<p>Unassessed</p>"
    parts = [f"<p>{escape(evidence['description'])}</p>"]
    for test in evidence["tests"]:
        body = "\n".join(
            f"{label}: {test[key]}"
            for key, label in (
                ("status", "Status"),
                ("code_desc", "Check"),
                ("message", "Observed result"),
                ("skip_message", "Skip reason"),
                ("exception", "Error"),
            )
            if test[key]
        )
        parts.append(f"<pre>{escape(body)}</pre>")
    if not evidence["test_count"]:
        parts.append("<p>No assertions recorded.</p>")
    if evidence["tests_truncated"]:
        parts.append(
            f"<p>Showing {MAX_CONTROL_TESTS} of {evidence['test_count']} assertions. "
            "Full results remain in the source evidence.</p>"
        )
    return "".join(parts)


def _facts_html(facts: dict[str, Any]) -> str:
    return (
        "<dl>"
        + "".join(
            f"<dt>{escape(label)}</dt><dd>{escape(str(value)) if value is not None else 'Unknown'}</dd>"
            for label, value in facts.items()
        )
        + "</dl>"
    )


def _summary_html(report: dict[str, Any]) -> str:
    run, asset = report["run"], report["asset"]
    baseline = report["baseline"] or {}
    sections = [
        _facts_html(
            {
                "Asset": asset["host"],
                "SSH endpoint": f"{asset['user']}@{asset['host']}:{asset['port']}",
                "Baseline": run["label"],
                "Baseline version": baseline.get("version"),
                "Action": run["action"],
                "Started": run["created_at"],
                "Last updated": run["updated_at"],
            }
        ),
        f"<p>{escape(str(run['detail']))}</p>",
    ]
    for phase in ("before", "after"):
        assessment = report["assessments"][phase]
        sections.append(f"<h2>{phase.capitalize()} assessment</h2>")
        if assessment is None:
            sections.append("<p>Unassessed. No evidence recorded for this phase.</p>")
            continue
        sections.append(
            _facts_html(
                {
                    "Execution": assessment.get("status"),
                    "Evidence origin": assessment.get("evidence_status"),
                    "Scanner version": assessment.get("scanner_version"),
                    "Profiles": ", ".join(
                        f"{p['name']} ({p['version'] or 'version unknown'})"
                        for p in assessment["profiles"]
                    ),
                    "Outcomes": ", ".join(
                        f"{count} {status.replace('_', ' ')}"
                        for status, count in assessment["counts"].items()
                    ),
                    "Inconclusive results": assessment["has_inconclusive_results"],
                }
            )
        )
    remediation = report["remediation"]
    sections.append("<h2>Remediation execution</h2>")
    sections.append(
        _facts_html(
            {
                "Successful execution": remediation.get("success"),
                "Check mode": remediation.get("check"),
                "Exit code": remediation.get("exit_code"),
                "Timed out": remediation.get("timed_out"),
            }
        )
        if remediation is not None
        else "<p>No remediation result recorded.</p>"
    )
    return "".join(sections)


def export_report(
    workspace: Path,
    record: dict[str, Any],
    format: str,
    *,
    evidence: ReviewEvidence | None = None,
) -> dict[str, str]:
    if format not in {"html", "json"}:
        raise ValueError("Choose HTML or JSON report export")
    report = build_report(workspace, record, evidence=evidence)
    if format == "json":
        body = json.dumps(report, indent=2, ensure_ascii=True)
        mime = "application/json"
    else:
        metadata = {key: value for key, value in report.items() if key != "controls"}
        rows = []
        for control in report["controls"]:
            rows.append(
                '<tr><td data-label="Control / impact">'
                + "<br>".join(
                    escape("Unknown" if control.get(key) is None else str(control[key]))
                    for key in ("profile", "control_id", "title", "impact")
                )
                + "</td>"
                + "".join(
                    f'<td data-label="{phase.capitalize()}"><strong>{escape(control[phase].replace("_", " "))}</strong>'
                    + _evidence_html(control[f"{phase}_evidence"])
                    + "</td>"
                    for phase in ("before", "after")
                )
                + f'<td data-label="Remediation mapping">{escape(control["remediation"])}</td></tr>'
            )
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            "<title>Kerkov compliance assessment</title><style>"
            "body{font:15px/1.6 system-ui,sans-serif;color:#182434;margin:2rem auto;max-width:1200px;padding:0 1rem}"
            "table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{border:1px solid #ccd3db;padding:1rem;text-align:left;vertical-align:top}"
            "th:first-child{width:18%}th:nth-child(2),th:nth-child(3){width:28%}th:last-child{width:26%}td>strong{white-space:nowrap}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}td{overflow-wrap:anywhere}"
            "dl{display:grid;grid-template-columns:minmax(120px,1fr) 3fr;gap:.4rem 1rem}dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}"
            ".controls{overflow-x:auto}th{background:#f2f5f8}h2{margin-top:2rem}"
            "@media screen and (max-width:700px){.controls table,.controls tbody,.controls tr,.controls td{display:block;width:auto}.controls thead{display:none}.controls tr{border:1px solid #ccd3db;margin:1rem 0}.controls td{border:0;border-bottom:1px solid #e4e8ed}.controls td:before{content:attr(data-label);display:block;font-weight:600;margin-bottom:.5rem}}"
            "@media print{body{margin:0;max-width:none}tr{break-inside:avoid}}"
            "</style></head><body><h1>Kerkov compliance assessment</h1>"
            f"<p>{escape(str(report['asset']['host']))} · {escape(str(report['run']['label']))}</p>"
            f"<p>Workflow outcome: <strong>{escape(report['run']['status'])}</strong></p>"
            f"<p>{escape(NOTICE)}</p>"
            + _summary_html(report)
            + "<details><summary>Full provenance and evidence references</summary>"
            f"<pre>{escape(json.dumps(metadata, indent=2, ensure_ascii=True))}</pre></details>"
            f'<h2>Control outcomes ({len(rows)})</h2><div class="controls"><table><thead><tr>'
            "<th>Control / impact</th><th>Before</th><th>After</th><th>Remediation mapping</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div></body></html>"
        )
        mime = "text/html"
    if len(body.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError(
            "Report exceeds the 16 MiB export limit; review the local evidence artifacts"
        )
    return {
        "body": body,
        "mime": mime,
        "filename": f"kerkov-compliance-{record['id']}.{format}",
    }
