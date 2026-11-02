"""First-class compliance navigation and built-in baseline workflows."""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.resource_profile_browser_test import (
    browser,
    dashboard,
)
from tests.web.resource_profile_browser_test import emit as emit_socket
from tests.web.resource_profile_browser_test import (
    last_request,
    open_profile,
    profile_fixture,
    sent,
    snapshot,
)


def emit(page, event_type, payload):
    if event_type.startswith(("tool.", "assistant.")):
        request = last_request(page, "compliance.run")
        emit_socket(
            page,
            "compliance.event",
            {
                "message_id": request["message_id"],
                "event_type": event_type,
                "payload": payload,
            },
        )
    else:
        emit_socket(page, event_type, payload)


def open_compliance(page: Page):
    entries, rows = profile_fixture()
    snapshot(page, entries)
    page.locator("#compliance-tab").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    return entries, rows


def connect(page: Page):
    page.locator("#compliance-connection summary").click()
    for name, value in {
        "user": "audit",
        "port": "2222",
        "private_key_file": "/keys/audit key",
        "known_hosts_file": "/keys/trusted hosts",
    }.items():
        page.locator(f'#compliance-form [name="{name}"]').fill(value)


def saved_preview(page: Page):
    values = page.locator("#compliance-form").evaluate(
        "form => Object.fromEntries(new FormData(form))"
    )
    baseline_id = page.locator(
        '.compliance-baseline[aria-pressed="true"]'
    ).get_attribute("data-compliance-baseline")
    catalog = json.loads(page.locator("#compliance-baseline-catalog").text_content())
    baseline = next((b for b in catalog if b["id"] == baseline_id), None)
    scan = {
        "host": "example.com",
        "user": values["user"],
        "port": int(values["port"]),
        "private_key_file": values["private_key_file"],
        "known_hosts_file": values["known_hosts_file"],
        "sudo": "sudo" in values,
        "profile": baseline["profile"] if baseline else values["profile"],
    }
    remediation = (
        None
        if baseline
        else {
            "profile": values["automation_profile"],
            "playbook": values["playbook"],
            "limit": values["host_alias"],
            "vars_files": [values["vars_file"]] if values.get("vars_file") else [],
        }
    )
    return {
        "id": "a" * 32,
        "status": "previewed",
        "stage": "finished",
        "label": "Linux account protection",
        "created_at": "2026-11-01T20:00:00Z",
        "updated_at": "2026-11-01T20:01:00Z",
        "request": {
            "action": "preview",
            "resource_id": 1,
            "baseline_id": baseline_id if baseline else None,
            "scan": scan,
            "remediation": remediation,
        },
        "detail": "Review the saved plan before applying.",
        "plan": {"changes": "Fix ownership and permissions."},
        "readiness": [
            {"name": "Docker", "status": "ready", "detail": "Daemon reachable"}
        ],
        "controls": [
            {
                "control_id": "passwd-protected",
                "title": "Protect account file",
                "profile": "accounts",
                "impact": 0.7,
                "before": "failed",
                "after": "unassessed",
            }
        ],
    }


def review_saved_preview(page: Page):
    emit_socket(
        page,
        "compliance.snapshot",
        {"context_id": "context-1", "runs": [saved_preview(page)]},
    )
    page.locator("#compliance-review-apply").click()


def _create_mock_access(port=2222, entry_id=40, **fields):
    return {
        "id": entry_id,
        "complete": True,
        "fields": {
            "user": "root",
            "port": port,
            "private_key_file": "/keys/from-setup",
            "known_hosts_file": "/keys/trusted-setup",
            "sudo": False,
            **fields,
        },
    }


def emit_access(page, candidates, request=None):
    request = request or last_request(page, "inventory.ssh_access")
    emit(
        page,
        "inventory.ssh_access",
        {
            **request,
            "resource": {"kind": "domain", "value": "example.com"},
            "candidates": candidates,
            "truncated": False,
        },
    )


def test_known_ssh_access_is_retrieved_and_used_without_retyping(dashboard: Page):
    page = dashboard
    open_compliance(page)
    emit_access(page, [_create_mock_access()])
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("root")
    expect(page.locator('#compliance-form [name="port"]')).to_have_value("2222")
    expect(page.locator("#compliance-connection-summary")).to_have_text(
        "Access from engagement"
    )
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")
    scan = request["workflow_request"]["scan"]
    assert scan["private_key_file"] == "/keys/from-setup"
    assert scan["known_hosts_file"] == "/keys/trusted-setup"
    assert scan["sudo"] is False


def test_multiple_container_ports_need_an_explicit_choice(dashboard: Page):
    page = dashboard
    open_compliance(page)
    emit_access(page, [_create_mock_access(), _create_mock_access(2233, 41)])
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("")
    page.locator("#compliance-connection summary").click()
    page.locator("#compliance-access-select").select_option("41")
    expect(page.locator('#compliance-form [name="port"]')).to_have_value("2233")


def test_access_lookup_never_overwrites_manual_connection_edits(dashboard: Page):
    page = dashboard
    open_compliance(page)
    connect(page)
    emit_access(page, [_create_mock_access()])
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("audit")
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value(
        "/keys/audit key"
    )
    page.locator("#compliance-access-select").select_option("40")
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("root")


def test_late_access_reply_cannot_cross_engagements(dashboard: Page):
    page = dashboard
    open_compliance(page)
    old = last_request(page, "inventory.ssh_access")
    entries, _ = profile_fixture()
    snapshot(page, entries, context_id="replacement-engagement")
    emit_access(page, [_create_mock_access()], request=old)
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value("")


def test_find_known_access_reads_existing_memory_without_scanning(dashboard: Page):
    page = dashboard
    open_compliance(page)
    emit_access(page, [])
    page.locator("#compliance-connection summary").click()
    page.locator("#compliance-access-find").click()
    message = last_request(page, "compliance.run")["message"]
    assert "use recall" in message and "kind='ssh'" in message
    assert "do not start/stop containers, scan, authenticate, remediate" in message
    assert "never guess from a Docker mapping" in message
    expect(page.locator("#compliance-view")).to_be_visible()


def test_access_refresh_is_not_lost_while_an_older_lookup_is_pending(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page)
    old = last_request(page, "inventory.ssh_access")
    snapshot(page, entries)
    emit_access(page, [], request=old)
    page.wait_for_function(
        "window.testSocket.sent.filter(item => item.type === 'inventory.ssh_access').length > 1"
    )
    current = last_request(page, "inventory.ssh_access")
    assert current["request_id"] != old["request_id"]
    emit_access(page, [_create_mock_access()], request=current)
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value(
        "/keys/from-setup"
    )


def test_compliance_workflow_and_host_profiles_link_to_it(dashboard: Page):
    page = dashboard
    expect(page.locator(".workspace-tabs > button").first).to_have_attribute(
        "id", "conversation-tab"
    )
    page.locator("#compliance-welcome").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    expect(page.locator("#compliance-empty")).to_be_visible()
    page.locator("#conversation-tab").click()
    open_profile(page)
    page.locator("#profile-compliance").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    expect(page.locator('[data-compliance-asset="1"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def test_builtin_baselines_need_no_profile_or_playbook_setup(dashboard: Page):
    page = dashboard
    open_compliance(page)
    expect(page.locator("#compliance-custom")).to_be_hidden()
    expect(page.locator('[data-compliance-baseline="linux-accounts"]')).to_contain_text(
        "4 checks · fixes included"
    )
    expect(page.locator('[data-compliance-baseline="ssh-essentials"]')).to_contain_text(
        "5 checks · fixes included"
    )
    connect(page)
    review_saved_preview(page)
    page.locator('#compliance-form button[type="submit"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    scan = request["scan"]
    assert scan["host"] == "example.com"
    assert scan["profile"].endswith("/compliance/linux-accounts")
    assert scan["sudo"] is True
    assert request["action"] == "apply"
    assert request["baseline_id"] == "linux-accounts"
    assert request["remediation"] is None
    assert request["preview_id"] == "a" * 32
    expect(page.locator("#compliance-view")).to_be_visible()


@pytest.mark.parametrize("baseline", ["linux-accounts", "ssh-essentials"])
def test_assessment_never_requests_remediation(dashboard: Page, baseline):
    page = dashboard
    page.locator("#message-input").fill("My unfinished draft")
    open_compliance(page)
    page.locator(f'[data-compliance-baseline="{baseline}"]').click()
    connect(page)
    page.get_by_role("button", name="Assess asset", exact=True).click()
    request = last_request(page, "compliance.run")
    assert request["workflow_request"]["action"] == "assess"
    assert request["workflow_request"]["remediation"] is None
    emit(
        page,
        "user.message.accepted",
        {"message_id": request["message_id"], "stoppable": True},
    )
    expect(page.locator("#message-input")).to_have_value("My unfinished draft")
    expect(page.locator(".compliance-request-details")).to_have_count(0)
    expect(page.locator("#compliance-view")).to_be_visible()
    page.locator("#compliance-stop").click()
    assert (
        last_request(page, "user.message.stop")["message_id"] == request["message_id"]
    )


def test_preview_requests_check_mode_and_retains_connection_on_baseline_switch(
    dashboard: Page,
):
    page = dashboard
    open_compliance(page)
    connect(page)
    page.locator('[data-compliance-baseline="ssh-essentials"]').click()
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("audit")
    page.get_by_role("button", name="Preview changes", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "preview"
    assert request["baseline_id"] == "ssh-essentials"
    assert request["scan"]["user"] == "audit"


def test_connection_validation_and_custom_host_alias(dashboard: Page):
    page = dashboard
    open_compliance(page)
    page.locator('#compliance-form button[type="submit"]').click()
    assert sent(page, "compliance.run") == []
    expect(page.locator("#compliance-connection")).to_have_attribute("open", "")
    for name, value in {
        "user": "audit",
        "private_key_file": "/keys/key",
        "known_hosts_file": "/keys/known_hosts",
    }.items():
        page.locator(f'#compliance-form [name="{name}"]').fill(value)
    page.locator('[data-compliance-baseline="custom"]').click()
    page.locator("#compliance-custom summary").click()
    for name, value in {
        "profile": "profiles/custom",
        "automation_profile": "linux_lab",
        "playbook": "hardening",
        "host_alias": "all",
    }.items():
        page.locator(f'#compliance-form [name="{name}"]').fill(value)
    page.locator('#compliance-form button[type="submit"]').click()
    assert sent(page, "compliance.run") == []
    expect(page.locator("#compliance-status")).to_contain_text("single host alias")
    page.locator('#compliance-form [name="host_alias"]').fill("server_one")
    review_saved_preview(page)
    page.locator('#compliance-form button[type="submit"]').click()
    run = last_request(page, "compliance.run")["workflow_request"]["remediation"]
    assert run["profile"] == "linux_lab" and run["limit"] == "server_one"


def test_connection_clears_on_identity_change_and_results_clear_on_engagement_reset(
    dashboard: Page,
):
    page = dashboard
    entries, rows = open_compliance(page)
    connect(page)
    entries[0]["entry"]["value"] = "replacement.example.com"
    snapshot(page, entries)
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value("")
    expect(page.locator("#compliance-selection")).to_contain_text("Customer API")
    snapshot(page, [], context_id="next-engagement")
    expect(page.locator("#compliance-result")).to_be_hidden()
    expect(page.locator("#compliance-assets button")).to_have_count(0)


def test_workflow_result_never_uses_ansible_success_as_compliance(dashboard: Page):
    page = dashboard
    open_compliance(page)
    connect(page)
    review_saved_preview(page)
    page.locator('#compliance-form button[type="submit"]').click()
    request = last_request(page, "compliance.run")
    emit(page, "tool.result", {"name": "ansible", "success": True, "metadata": {}})
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    page.locator("#compliance-tab").click()
    expect(page.locator("#compliance-result-detail")).to_contain_text(
        "No verified assessment result received"
    )


def test_custom_assessment_ignores_unused_remediation_fields(dashboard: Page):
    page = dashboard
    open_compliance(page)
    connect(page)
    page.locator('[data-compliance-baseline="custom"]').click()
    page.locator("#compliance-custom summary").click()
    page.locator('#compliance-form [name="profile"]').fill("profiles/custom")
    page.locator('#compliance-form [name="host_alias"]').fill("invalid:group")
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess" and request["remediation"] is None


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_compliance_layout_and_disconnected_actions(dashboard: Page, width, theme):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_compliance(page)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.evaluate("window.testSocket.close()")
    expect(page.locator('#compliance-form button[type="submit"]')).to_be_disabled()
    expect(page.locator("#compliance-add-asset")).to_be_disabled()
    assert sent(page, "compliance.run") == []


@pytest.mark.parametrize("width", [1440, 390])
def test_workflow_progress_approvals_and_results_stay_out_of_chat(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    connect(page)
    before = page.locator("#message-stream").inner_text()
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")
    assert request["context_id"]
    emit(
        page,
        "user.message.accepted",
        {"message_id": request["message_id"], "stoppable": True},
    )
    emit(page, "tool.start", {"name": "compliance_scan", "params": {}})
    expect(page.locator("#compliance-status")).to_have_text("Assessing baseline…")
    emit(
        page,
        "approval.request",
        {
            "approval_id": "workflow-approval",
            "tool": "compliance_scan",
            "params": {"host": "example.com"},
        },
    )
    expect(page.locator("#compliance-approval-host #approval-tray")).to_be_visible()
    expect(page.locator("#compliance-view")).to_be_visible()
    page.locator("#approval-list").get_by_role(
        "button", name="Approve", exact=True
    ).click()
    assert last_request(page, "approval.response")["approval_id"] == "workflow-approval"
    emit(
        page,
        "approval.resolved",
        {"approval_id": "workflow-approval", "decision": "approve"},
    )
    emit(
        page,
        "assistant.message",
        {"message": "Assessment evidence: reports/check.json", "final": True},
    )
    expect(page.locator("#compliance-workflow-output")).to_contain_text(
        "reports/check.json"
    )
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    assert page.locator("#message-stream").inner_text() == before
    expect(page.locator("#compliance-stop")).to_be_hidden()
    page.screenshot(path=f"/tmp/kerkov-compliance-workflow-{width}.png", full_page=True)


def test_disconnect_does_not_leave_a_workflow_running_or_claim_success(dashboard: Page):
    page = dashboard
    open_compliance(page)
    connect(page)
    page.locator('[data-compliance-action="assess"]').click()
    page.evaluate("window.testSocket.close()")
    expect(page.locator("#compliance-result-title")).to_have_text(
        "Workflow connection lost"
    )
    expect(page.locator("#compliance-result-detail")).to_contain_text(
        "outcome is unknown"
    )
    expect(page.locator("#compliance-stop")).to_be_hidden()


def test_apply_requires_a_reviewed_preview(dashboard: Page):
    page = dashboard
    open_compliance(page)
    connect(page)
    page.locator('#compliance-form button[type="submit"]').click()
    assert sent(page, "compliance.run") == []
    expect(page.locator("#compliance-status")).to_contain_text("Preview changes first")
    review_saved_preview(page)
    expect(page.locator("#compliance-controls")).to_contain_text("passwd-protected")
    expect(page.locator("#compliance-controls")).to_contain_text("failed")
    expect(page.locator("#compliance-controls")).to_contain_text("unassessed")
    page.locator('#compliance-form button[type="submit"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["preview_id"] == "a" * 32
    assert request["action"] == "apply"
    assert request["scan"]["host"] == "example.com"


@pytest.mark.parametrize(
    "status,title",
    [
        ("previewed", "Preview ready for review"),
        ("verified", "Baseline verified"),
        ("passed", "Baseline checks passed"),
        ("attention", "Baseline needs attention"),
        ("interrupted", "Workflow interrupted"),
        ("failed", "Workflow needs review"),
        ("blocked", "Workflow blocked"),
        ("incomplete", "Workflow incomplete"),
    ],
)
def test_outcome_is_rendered_from_the_server_record(dashboard: Page, status, title):
    page = dashboard
    open_compliance(page)
    connect(page)
    record = saved_preview(page)
    record["status"] = status
    emit_socket(page, "compliance.updated", {"context_id": "context-1", "run": record})
    expect(page.locator("#compliance-result-title")).to_have_text(title)


@pytest.mark.parametrize("width", [390, 1440])
def test_saved_results_restore_with_controls_evidence_and_safe_reassessment(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    connect(page)
    record = saved_preview(page)
    record.update(
        status="interrupted",
        detail="Changes may have occurred. Reassess before retrying.",
    )
    record["before"] = {
        "normalized_artifact": "/workspace/artifacts/compliance/scan-fixture/normalized.json"
    }
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    expect(page.locator("#compliance-result-title")).to_have_text(
        "Workflow interrupted"
    )
    expect(page.locator("#compliance-history")).to_contain_text(
        "Linux account protection"
    )
    expect(page.locator("#compliance-controls")).to_contain_text("Protect account file")
    page.get_by_role("button", name="before evidence", exact=True).click()
    evidence = last_request(page, "compliance.evidence")
    assert evidence["run_id"] == record["id"]
    emit_socket(
        page,
        "compliance.evidence",
        {
            "context_id": "context-1",
            "run_id": record["id"],
            "body": "normalized evidence fixture",
        },
    )
    expect(page.locator("#compliance-evidence-preview")).to_have_text(
        "normalized evidence fixture"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"/tmp/kerkov-compliance-results-{width}.png", full_page=True)
    page.locator("#compliance-reassess").click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess"
    assert request["assessment_of"] == record["id"]
    assert request["remediation"] is None
    emit_socket(page, "compliance.snapshot", {"context_id": "old", "runs": []})
    expect(page.locator("#compliance-history")).to_contain_text(
        "Linux account protection"
    )
