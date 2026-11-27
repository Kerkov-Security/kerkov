"""Select remediation for failed rules from a pinned PCI DSS playbook."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from kerkov.core.compliance.review import ReviewEvidence
from kerkov.core.compliance.scap_modules import SCAP_MODULES

_RULE_PREFIX = "xccdf_org.ssgproject.content_rule_"
_NESTED_TASKS = ("block", "rescue", "always")
_SHARED_FACT_MODULES = {
    "ansible.builtin.package_facts",
    "ansible.builtin.service_facts",
}


def _controls(
    evidence: ReviewEvidence, workspace: Path, assessment: dict[str, Any]
) -> list[dict[str, Any]]:
    if (
        assessment.get("engine") != "openscap"
        or assessment.get("evidence_status") != "scanned"
    ):
        raise ValueError("Use a saved assessment scanned by Kerkov")
    report, _ = evidence.load(workspace, assessment)
    return report["controls"]


def failed_scap_rules(
    evidence: ReviewEvidence, workspace: Path, assessment: dict[str, Any]
) -> set[str]:
    """Get all failed rule IDs from the complete verified report."""
    return {
        control["control_id"]
        for control in _controls(evidence, workspace, assessment)
        if control["xccdf_result"] == "fail"
    }


def _review_reason(task: dict[str, Any], modules: set[str]) -> str | None:
    if task.keys() & {
        "delegate_to",
        "local_action",
        "connection",
        "action",
        "ansible.builtin.include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    }:
        return "This task needs a separately reviewed execution scope."
    for key in task:
        if "." in key and not key.startswith("ansible.builtin."):
            if key not in SCAP_MODULES:
                return "This fix requires a separately reviewed Ansible collection."
            modules.add(key)
    for group in _NESTED_TASKS:
        for child in task.get(group, []):
            reason = _review_reason(child, modules)
            if reason:
                return reason
    return None


def plan_scap_fixes(
    *,
    workspace: Path,
    evidence: ReviewEvidence,
    assessment: dict[str, Any],
    pinned_playbook: bytes,
    destination: Path,
    selected: list[str] | None,
    ssh_user: str,
) -> dict[str, Any]:
    """Write a single-host play from the matching, runnable rule task groups."""
    controls = _controls(evidence, workspace, assessment)
    tagged = {
        control["control_id"].removeprefix(_RULE_PREFIX): control["control_id"]
        for control in controls
        if control["control_id"].startswith(_RULE_PREFIX)
    }
    failed = {
        control["control_id"]: control["title"] or control["control_id"]
        for control in controls
        if control["xccdf_result"] == "fail"
    }
    if not failed:
        raise ValueError("The saved PCI DSS assessment has no failed checks")
    try:
        source = yaml.safe_load(pinned_playbook)
    except yaml.YAMLError as exc:
        raise ValueError("The pinned PCI DSS playbook is invalid") from exc
    if (
        not isinstance(source, list)
        or len(source) != 1
        or not isinstance(source[0], dict)
        or set(source[0]) != {"name", "hosts", "vars", "tasks"}
        or source[0]["hosts"] != "all"
        or not isinstance(source[0]["vars"], dict)
        or not isinstance(source[0]["tasks"], list)
    ):
        raise ValueError("The pinned PCI DSS playbook has an unsupported structure")
    play = source[0]
    reasons: dict[str, str] = {}
    rule_modules: dict[str, set[str]] = defaultdict(set)
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    shared_facts: list[tuple[int, dict[str, Any]]] = []
    for index, task in enumerate(play["tasks"]):
        tags = task.get("tags", [])
        rule_ids = {tagged[tag] for tag in tags if tag in tagged}
        if not rule_ids and "always" in tags:
            modules = task.keys() - {"name", "tags"}
            if len(modules) != 1 or not modules <= _SHARED_FACT_MODULES:
                raise ValueError(
                    "The pinned PCI DSS playbook contains an unsupported shared task"
                )
            shared_facts.append((index, task))
            continue
        for rule_id in rule_ids & failed.keys():
            groups[rule_id].append((index, task))
            if len(rule_ids) != 1:
                reasons[rule_id] = "This task is shared with another rule."
            elif reason := _review_reason(task, rule_modules[rule_id]):
                reasons[rule_id] = reason
    root_login = f"{_RULE_PREFIX}sshd_disable_root_login"
    if ssh_user == "root" and root_login in failed:
        reasons[root_login] = (
            "This fix would disable the SSH login used by the workflow."
        )

    available = []
    manual = []
    for rule_id, title in sorted(failed.items()):
        reason = reasons.get(rule_id)
        if reason or not groups[rule_id]:
            manual.append(
                {
                    "id": rule_id,
                    "title": title,
                    "reason": reason or "The pinned playbook has no matching task.",
                }
            )
        else:
            available.append(
                {
                    "id": rule_id,
                    "title": title,
                    "tasks": [task["name"] for _, task in groups[rule_id]],
                }
            )
    allowed = {rule["id"] for rule in available}
    selection = allowed if selected is None else set(selected)
    if selected is not None and (
        len(selection) != len(selected) or not selection <= allowed
    ):
        raise ValueError("Choose distinct PCI DSS checks with available fixes")
    tasks = (
        sorted([*shared_facts, *(item for rule in selection for item in groups[rule])])
        if selection
        else []
    )
    if tasks:
        destination.write_text(
            yaml.safe_dump(
                [
                    {
                        **play,
                        "hosts": "selected_asset",
                        "tasks": [task for _, task in tasks],
                    }
                ],
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    modules = {
        name: {
            "version": SCAP_MODULES[name].version,
            "sha256": SCAP_MODULES[name].sha256,
        }
        for name in sorted({name for rule in selection for name in rule_modules[rule]})
    }
    return {
        "modules": modules,
        "available": available,
        "manual": manual,
        "selected": sorted(selection),
        "task_count": len(tasks),
        "source_sha256": assessment["report_sha256"],
    }
