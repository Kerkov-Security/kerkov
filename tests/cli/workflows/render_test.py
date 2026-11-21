"""Terminal rendering of compliance run projections."""

from rich.console import Console

from kerkov.cli.workflows.render import control_summary, run_view, runs_table
from kerkov.core.compliance.workflows import WorkflowRequest, control_comparison


def _create_mock_projection(**updates):
    request = WorkflowRequest.model_validate(
        {
            "action": "apply",
            "resource_id": 1,
            "scan": {
                "host": "192.0.2.1",
                "user": "root",
                "profile": "/profiles/fixture",
                "private_key_file": "/private/key",
                "known_hosts_file": "/private/trust",
            },
        }
    )
    controls = [
        {"profile": "p", "control_id": "ssh-1", "title": "[red]Root[/red] login"},
        {"profile": "p", "control_id": "ssh-2", "title": "Password auth"},
    ]

    def report(statuses, artifact):
        counts = dict.fromkeys(
            ["passed", "failed", "error", "skipped", "waived", "no_tests"], 0
        )
        for status in statuses:
            counts[status] += 1
        return {
            "control_count": len(statuses),
            "counts": counts,
            "controls": [
                {**control, "status": status, "impact": 0.7}
                for control, status in zip(controls, statuses)
            ],
            "normalized_artifact": artifact,
        }

    record = {
        "id": "a" * 32,
        "request": request.model_dump(),
        "label": "Linux SSH baseline",
        "status": "verified",
        "stage": "finished",
        "detail": "All selected baseline checks passed.",
        "updated_at": "2026-11-16T10:00:00+00:00",
        "readiness": [
            {"name": "Docker daemon", "status": "ready", "detail": "Reachable."},
            {"name": "Ansible", "status": "blocked", "detail": "Missing."},
        ],
        "plan": {"changes": "Harden sshd_config."},
        "before": report(["failed", "skipped"], "/ws/artifacts/before.json"),
        "after": report(["passed", "passed"], "/ws/artifacts/after.json"),
        "remediation": {"log_artifact": "/ws/artifacts/ansible.log"},
        **updates,
    }
    record["controls"] = control_comparison(record)
    return record


def _render(renderable) -> str:
    console = Console(record=True, width=160, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_run_view_shows_outcome_stages_controls_and_evidence():
    text = _render(run_view(_create_mock_projection()))
    assert "Baseline verified · 192.0.2.1 · Linux SSH baseline" in text
    assert "readiness › assessment › remediation › verification  (finished)" in text
    assert "✓ Docker daemon: Reachable." in text
    assert "✗ Ansible: Missing." in text
    assert "Plan: Harden sshd_config." in text
    assert "2 passed · 0 failed" in text
    assert "ssh-2" in text and "skipped" in text
    assert "before: /ws/artifacts/before.json" in text
    assert "after: /ws/artifacts/after.json" in text
    assert "remediation: /ws/artifacts/ansible.log" in text


def test_record_strings_are_never_parsed_as_markup():
    text = _render(run_view(_create_mock_projection()))
    assert "[red]Root[/red] login" in text


def test_run_without_evidence_says_so():
    record = _create_mock_projection(before=None, after=None, remediation=None)
    text = _render(run_view(record))
    assert "No verified control evidence received yet." in text
    assert "Evidence:" not in text


def test_runs_table_shortens_ids_and_summarizes_the_latest_report():
    record = _create_mock_projection()
    text = _render(runs_table([record]))
    assert "a" * 12 in text and "a" * 13 not in text
    assert "2/2 passed" in text
    assert control_summary({**record, "after": None}) == "0/2 passed"
    assert control_summary({**record, "after": None, "before": None}) == "—"
