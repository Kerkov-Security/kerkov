"""Render compliance run projections for the terminal."""

from typing import Any, Dict, List

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

# Mirrors the browser panel's titles and stage sequence (host-compliance.js) so
# both front ends describe the same record the same way.
STATUS_TITLES: Dict[str, str] = {
    "running": "Workflow in progress",
    "previewed": "Preview ready for review",
    "verified": "Baseline verified",
    "passed": "Baseline checks passed",
    "attention": "Baseline needs attention",
    "interrupted": "Workflow interrupted",
    "failed": "Workflow needs review",
    "blocked": "Workflow blocked",
    "incomplete": "Workflow incomplete",
}
STATUS_STYLES: Dict[str, str] = {
    "running": "cyan",
    "previewed": "cyan",
    "verified": "green",
    "passed": "green",
    "attention": "yellow",
    "interrupted": "yellow",
    "incomplete": "yellow",
    "failed": "red",
    "blocked": "red",
}
OUTCOME_STYLES: Dict[str, str] = {
    "passed": "green",
    "failed": "red",
    "error": "yellow",
    "skipped": "yellow",
    "waived": "yellow",
    "no_tests": "yellow",
    "unassessed": "dim",
}
STAGES: Dict[str, List[str]] = {
    "assess": ["readiness", "assessment"],
    "preview": ["readiness", "assessment", "preview"],
    "apply": ["readiness", "assessment", "remediation", "verification"],
}
EVIDENCE_KEYS = {
    "before": "normalized_artifact",
    "after": "normalized_artifact",
    "remediation": "log_artifact",
}
SHORT_ID = 12


def status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLES.get(status, ""))


def outcome_text(outcome: str) -> Text:
    return Text(outcome.replace("_", " "), style=OUTCOME_STYLES.get(outcome, ""))


def control_summary(record: Dict[str, Any]) -> str:
    report = record.get("after") or record.get("before")
    if not report or not report.get("control_count"):
        return "—"
    return f"{report['counts'].get('passed', 0)}/{report['control_count']} passed"


def runs_table(records: List[Dict[str, Any]]) -> Table:
    table = Table(title="Compliance runs", border_style="dim")
    table.add_column("Run", style="cyan", no_wrap=True)
    table.add_column("Action")
    table.add_column("Host", overflow="fold")
    table.add_column("Baseline", overflow="fold")
    table.add_column("Status")
    table.add_column("Controls", justify="right")
    table.add_column("Updated", style="dim", no_wrap=True)
    for record in records:
        request = record.get("request", {})
        table.add_row(
            record["id"][:SHORT_ID],
            str(request.get("action", "")),
            Text(str(request.get("scan", {}).get("host", ""))),
            Text(str(record.get("label", ""))),
            status_text(str(record.get("status", ""))),
            control_summary(record),
            str(record.get("updated_at", ""))[:19].replace("T", " "),
        )
    return table


def stage_line(record: Dict[str, Any]) -> Text:
    stages = STAGES.get(record.get("request", {}).get("action", ""), [])
    line = Text()
    for index, stage in enumerate(stages):
        if index:
            line.append(" › ", style="dim")
        line.append(stage, style="bold" if stage == record.get("stage") else "dim")
    if record.get("stage") in {"finished", "blocked"}:
        line.append(f"  ({record['stage']})", style="dim")
    return line


def controls_table(controls: List[Dict[str, Any]]) -> Table:
    table = Table(border_style="dim", show_edge=False)
    table.add_column("Control", style="cyan", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Impact", justify="right")
    table.add_column("Before")
    table.add_column("After")
    for control in controls:
        impact = control.get("impact")
        table.add_row(
            Text(str(control.get("control_id", ""))),
            Text(str(control.get("title") or control.get("profile") or "")),
            "Unknown" if impact is None else str(impact),
            outcome_text(str(control.get("before", "unassessed"))),
            outcome_text(str(control.get("after", "unassessed"))),
        )
    return table


def evidence_paths(record: Dict[str, Any]) -> List[str]:
    paths = []
    for phase, key in EVIDENCE_KEYS.items():
        path = (record.get(phase) or {}).get(key)
        if path:
            paths.append(f"{phase}: {path}")
    return paths


def run_view(record: Dict[str, Any]) -> RenderableType:
    """One run: outcome, stage progress, readiness, controls and evidence."""
    status = str(record.get("status", ""))
    request = record.get("request", {})
    parts: List[RenderableType] = []

    heading = Text(STATUS_TITLES.get(status, status), style="bold")
    heading.stylize(STATUS_STYLES.get(status, ""))
    heading.append(f" · {request.get('scan', {}).get('host', '')}", style="")
    heading.append(f" · {record.get('label', '')}", style="")
    parts.append(heading)
    parts.append(Text(str(record.get("detail", ""))))
    parts.append(
        Text(
            f"Run {record.get('id', '')} · {request.get('action', '')} · "
            f"updated {str(record.get('updated_at', ''))[:19].replace('T', ' ')}",
            style="dim",
        )
    )
    parts.append(stage_line(record))

    for check in record.get("readiness") or []:
        ready = check.get("status") == "ready"
        line = Text("✓ " if ready else "✗ ", style="green" if ready else "red")
        line.append(f"{check.get('name', '')}: {check.get('detail', '')}")
        parts.append(line)

    changes = (record.get("plan") or {}).get("changes")
    if changes:
        parts.append(Text(f"Plan: {changes}", style="dim"))

    report = record.get("after") or record.get("before")
    if report and report.get("counts"):
        parts.append(
            Text(
                " · ".join(
                    f"{count} {name.replace('_', ' ')}"
                    for name, count in report["counts"].items()
                )
            )
        )
    controls = record.get("controls") or []
    if controls:
        parts.append(controls_table(controls))
        parts.append(
            Text(
                "An unassessed after column means verification has not been "
                "recorded.",
                style="dim",
            )
        )
    else:
        parts.append(Text("No verified control evidence received yet.", style="dim"))

    paths = evidence_paths(record)
    if paths:
        parts.append(Text("Evidence:", style="bold"))
        parts.extend(Text(f"  {path}") for path in paths)
    return Group(*parts)
