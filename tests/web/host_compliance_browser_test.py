"""First-class compliance navigation and built-in baseline workflows."""

import json
import sys

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.resource_profile_browser_test import emit as emit_socket
from tests.web.resource_profile_browser_test import (
    last_request,
    open_profile,
    profile_fixture,
    sent,
    snapshot,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Compliance workflow fixtures require a POSIX controller",
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


def open_compliance(page: Page, *, configure=False):
    entries, rows = profile_fixture()
    snapshot(page, entries)
    page.locator("#compliance-tab").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    if configure:
        page.locator("#compliance-customize").click()
        page.locator("#compliance-profile-options > summary").click()
    return entries, rows


def connect(page: Page):
    if not page.locator("#compliance-connection").evaluate("node => node.open"):
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
            "bootstrap_python": "bootstrap_python" in values,
        },
        "detail": "Review the saved plan before applying.",
        "plan": {
            "changes": "Fix ownership and permissions.",
            "profile": "/workspace/profile",
        },
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


def show_failed_assessment(page: Page):
    record = saved_preview(page)
    record.update(status="attention", before={"counts": {"passed": 3, "failed": 1}})
    record["request"]["action"] = "assess"
    emit_socket(
        page,
        "compliance.snapshot",
        {"context_id": "context-1", "runs": [record]},
    )
    return record


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
    open_compliance(page, configure=True)
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


@pytest.mark.parametrize("width", [390, 1440])
def test_multiple_container_ports_are_selectable_in_conversation(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    emit_access(page, [_create_mock_access(), _create_mock_access(2233, 41)])
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("")
    picker = page.get_by_label("SSH endpoint")
    expect(picker).to_be_visible()
    expect(page.locator("#compliance-customize")).to_have_attribute(
        "aria-expanded", "false"
    )
    scan = page.get_by_role("button", name="Scan host", exact=True)
    scan.click()
    card = page.get_by_role("article", name="Compliance assessment")
    expect(card).to_contain_text("Which SSH endpoint should I assess?")
    assert sent(page, "compliance.run") == []
    option = card.get_by_role("button", name="example.com:2233 · root")
    option.click()
    expect(page.locator('#compliance-form [name="port"]')).to_have_value("2233")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["scan"]["port"] == 2233


def test_assessment_records_a_new_host_from_conversation(dashboard: Page):
    page = dashboard
    snapshot(page, [])
    page.locator("#compliance-tab").click()
    page.get_by_role("button", name="Scan host", exact=True).click()
    card = page.get_by_role("article", name="Compliance assessment")
    expect(card).to_contain_text("Which host should I assess?")
    card.get_by_label("Hostname or IP address").fill("192.0.2.40")
    card.get_by_role("button", name="Use host").click()
    save = last_request(page, "inventory.save")
    assert save["resource"] == {"kind": "ip", "value": "192.0.2.40"}
    emit_socket(
        page,
        "inventory.saved",
        {"context_id": "context-1", "request_id": save["request_id"], "entry_id": 9},
    )
    entries, _ = profile_fixture()
    new_host = dict(entries[0], id=9, section="assets", entry=save["resource"])
    emit_socket(
        page,
        "inventory.snapshot",
        {
            "context_id": "context-1",
            "request_id": save["request_id"],
            "available": True,
            "entries": [new_host],
        },
    )
    lookup = last_request(page, "inventory.ssh_access")
    assert lookup["entry_id"] == 9
    emit_socket(
        page,
        "inventory.ssh_access",
        {
            **lookup,
            "resource": save["resource"],
            "candidates": [_create_mock_access()],
        },
    )
    run = last_request(page, "compliance.run")["workflow_request"]
    assert run["resource_id"] == 9
    assert run["scan"]["host"] == "192.0.2.40"
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(card).to_contain_text("192.0.2.40:2222")


def test_assessment_prompts_in_conversation_when_multiple_hosts_exist(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page)
    entries.append(
        dict(entries[0], id=9, entry={"kind": "domain", "value": "other.example.com"})
    )
    snapshot(page, entries)
    page.get_by_role("button", name="Scan host", exact=True).click()
    card = page.get_by_role("article", name="Compliance assessment")
    expect(card).to_contain_text("Which host should I assess?")
    assert not sent(page, "compliance.run")
    card.get_by_role("button", name="other.example.com", exact=True).click()
    lookup = last_request(page, "inventory.ssh_access")
    assert lookup["entry_id"] == 9
    emit_socket(
        page,
        "inventory.ssh_access",
        {
            **lookup,
            "resource": {"kind": "domain", "value": "other.example.com"},
            "candidates": [_create_mock_access()],
        },
    )
    assert last_request(page, "compliance.run")["workflow_request"]["resource_id"] == 9


def test_switching_ssh_endpoints_from_a_result_prepares_a_new_assessment(
    dashboard: Page,
):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    old_run = show_failed_assessment(page)
    emit_access(
        page,
        [
            _create_mock_access(
                2222,
                40,
                user="audit",
                private_key_file="/keys/audit key",
                known_hosts_file="/keys/trusted hosts",
                sudo=True,
            ),
            _create_mock_access(2233, 41),
        ],
    )
    picker = page.get_by_label("SSH endpoint")
    expect(picker).to_have_value("40")
    expect(page.locator("#compliance-result")).to_be_visible()
    picker.select_option("41")
    expect(page.locator("#compliance-result")).to_be_hidden()
    expect(page.locator("#compliance-setup")).to_have_attribute("open", "")
    expect(page.locator('[name="port"]')).to_have_value("2233")
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [old_run]}
    )
    expect(page.locator("#compliance-result")).to_be_hidden()
    assert not sent(page, "compliance.run")


def test_access_lookup_never_overwrites_manual_connection_edits(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
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
    open_compliance(page, configure=True)
    old = last_request(page, "inventory.ssh_access")
    entries, _ = profile_fixture()
    snapshot(page, entries, context_id="replacement-engagement")
    emit_access(page, [_create_mock_access()], request=old)
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value("")


def test_find_known_access_reads_existing_memory_without_scanning(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    emit_access(page, [])
    page.locator("#compliance-access-find").click()
    message = last_request(page, "compliance.run")["message"]
    assert "use recall" in message and "kind='ssh'" in message
    assert "do not start/stop containers, scan, authenticate, remediate" in message
    assert "never guess from a Docker mapping" in message
    expect(page.locator("#compliance-view")).to_be_visible()


def test_access_refresh_is_not_lost_while_an_older_lookup_is_pending(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page, configure=True)
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
    expect(page.locator("#compliance-host")).to_be_disabled()
    page.locator("#conversation-tab").click()
    open_profile(page)
    page.locator("#profile-compliance").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    expect(page.locator("#compliance-host")).to_have_value("1")


def test_builtin_baselines_need_no_profile_or_playbook_setup(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    expect(page.locator("#compliance-custom")).to_be_hidden()
    expect(page.locator('[data-compliance-baseline="linux-accounts"]')).to_contain_text(
        "4 checks · fixes included"
    )
    expect(page.locator('[data-compliance-baseline="ssh-essentials"]')).to_contain_text(
        "5 checks · fixes included"
    )
    connect(page)
    review_saved_preview(page)
    page.get_by_role("button", name="Apply fixes", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    scan = request["scan"]
    assert scan["host"] == "example.com"
    assert scan["profile"].endswith("/compliance/linux-accounts")
    assert scan["sudo"] is True
    assert request["action"] == "apply"
    assert request["baseline_id"] == "linux-accounts"
    assert request["remediation"] is None
    assert request["preview_id"] == "a" * 32
    expect(page.locator("#conversation-view")).to_be_visible()


@pytest.mark.parametrize("baseline", ["linux-accounts", "ssh-essentials"])
def test_assessment_never_requests_remediation(dashboard: Page, baseline):
    page = dashboard
    page.locator("#message-input").fill("My unfinished draft")
    open_compliance(page, configure=True)
    page.locator(f'[data-compliance-baseline="{baseline}"]').click()
    connect(page)
    page.get_by_role("button", name="Scan host", exact=True).click()
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
    expect(page.locator("#conversation-view")).to_be_visible()
    page.get_by_role("button", name="Stop main run").click()
    assert (
        last_request(page, "user.message.stop")["message_id"] == request["message_id"]
    )


def test_preview_requests_check_mode_and_retains_connection_on_baseline_switch(
    dashboard: Page,
):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="ssh-essentials"]').click()
    expect(page.locator('#compliance-form [name="user"]')).to_have_value("audit")
    record = saved_preview(page)
    show_failed_assessment(page)
    page.get_by_role("button", name="Preview fixes", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "preview"
    assert request["baseline_id"] == "ssh-essentials"
    assert request["scan"]["user"] == "audit"
    preview = page.get_by_role("article", name="Compliance preview")
    expect(preview).to_contain_text("example.com:2222")
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(preview.get_by_role("button", name="Apply fixes")).to_have_count(0)
    message_id = last_request(page, "compliance.run")["message_id"]
    emit(page, "user.message.accepted", {"message_id": message_id, "stoppable": True})
    expect(page.get_by_role("button", name="Stop main run")).to_be_visible()
    emit(page, "tool.start", {"name": "ansible", "params": {"check": True}})
    expect(preview).to_contain_text("Previewing changes")
    emit(
        page,
        "approval.request",
        {
            "approval_id": "preview-approval",
            "tool": "ansible",
            "params": {"check": True},
        },
    )
    expect(page.locator("#conversation-approval-host #approval-tray")).to_be_visible()
    emit(
        page,
        "approval.resolved",
        {"approval_id": "preview-approval", "decision": "approve"},
    )

    record = {
        **record,
        "request": request,
        "remediation": {
            "totals": {"changed": 1},
            "changed_tasks": [
                {"task": "Install managed SSH drop-in", "host": "selected_asset"}
            ],
        },
        "plan": {
            "baseline": {
                "remediation": {"ssh-no-x11": "Set global X11Forwarding to no."}
            },
        },
        "controls": [
            {
                "control_id": "ssh-no-x11",
                "title": "Disable X11 forwarding",
                "before": "failed",
                "after": "unassessed",
            }
        ],
    }
    emit(
        page, "assistant.message", {"message": "SSH preview narration.", "final": True}
    )
    expect(page.locator("#message-stream")).to_contain_text("SSH preview narration.")
    expect(page.locator("#compliance-workflow-output")).not_to_contain_text(
        "SSH preview narration."
    )
    emit_socket(
        page,
        "compliance.updated",
        {"context_id": "context-1", "message_id": message_id, "run": record},
    )
    emit(page, "user.message.complete", {"message_id": message_id})
    expect(preview).to_contain_text("Ansible check mode predicts 1 task change.")
    expect(preview).to_contain_text("Install managed SSH drop-in")
    expect(preview).to_contain_text("Set global X11Forwarding to no.")
    expect(page.locator("#message-stream > :last-child")).to_have_attribute(
        "aria-label", "Compliance preview"
    )
    preview.get_by_role("button", name="View preview evidence").click()
    page.get_by_role("button", name="Review fixes").click()
    expect(preview).to_have_count(1)
    preview.get_by_role("button", name="Apply fixes").click()
    applied = last_request(page, "compliance.run")
    assert applied["workflow_request"]["preview_id"] == record["id"]
    assert applied["workflow_request"]["scan"] == request["scan"]
    remediation = page.get_by_role("article", name="Compliance remediation")
    expect(remediation).to_contain_text("example.com:2222")
    emit(page, "tool.start", {"name": "ansible", "params": {"check": False}})
    expect(remediation).to_contain_text("Applying baseline")
    applied_record = {
        **record,
        "id": "b" * 32,
        "status": "verified",
        "detail": "All selected baseline checks passed.",
        "request": applied["workflow_request"],
        "after": {"counts": {"passed": 5, "failed": 0, "error": 0}},
    }
    emit_socket(
        page,
        "compliance.updated",
        {
            "context_id": "context-1",
            "message_id": applied["message_id"],
            "run": applied_record,
        },
    )
    emit(page, "user.message.complete", {"message_id": applied["message_id"]})
    expect(remediation).to_contain_text("Install managed SSH drop-in")
    expect(remediation).to_contain_text(
        "Follow-up assessment found 5 passed, 0 failed, and 0 errored checks."
    )
    remediation.get_by_role("button", name="View remediation results").click()
    expect(page.locator("#compliance-result-title")).to_have_text("Baseline verified")


def test_custom_remediation_requires_a_single_host_alias(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
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
    show_failed_assessment(page)
    page.locator("#compliance-result-preview").click()
    assert sent(page, "compliance.run") == []
    expect(page.locator("#compliance-status")).to_contain_text("single host alias")
    page.locator('#compliance-form [name="host_alias"]').fill("server_one")
    review_saved_preview(page)
    page.get_by_role("button", name="Apply fixes", exact=True).click()
    run = last_request(page, "compliance.run")["workflow_request"]["remediation"]
    assert run["profile"] == "linux_lab" and run["limit"] == "server_one"


def test_connection_clears_on_identity_change_and_results_clear_on_engagement_reset(
    dashboard: Page,
):
    page = dashboard
    entries, rows = open_compliance(page, configure=True)
    connect(page)
    entries[0]["entry"]["value"] = "replacement.example.com"
    snapshot(page, entries)
    expect(page.locator('#compliance-form [name="private_key_file"]')).to_have_value("")
    expect(page.locator("#compliance-selection")).to_contain_text("Customer API")
    snapshot(page, [], context_id="next-engagement")
    expect(page.locator("#compliance-result")).to_be_hidden()
    expect(page.locator("#compliance-host")).to_have_value("")


def test_workflow_result_never_uses_ansible_success_as_compliance(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    review_saved_preview(page)
    page.get_by_role("button", name="Apply fixes", exact=True).click()
    request = last_request(page, "compliance.run")
    emit(page, "tool.result", {"name": "ansible", "success": True, "metadata": {}})
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    page.locator("#compliance-tab").click()
    expect(page.locator("#compliance-result-detail")).to_contain_text(
        "No verified assessment result received"
    )


def test_custom_assessment_ignores_unused_remediation_fields(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="custom"]').click()
    page.locator("#compliance-custom summary").click()
    page.locator('#compliance-form [name="profile"]').fill("profiles/custom")
    page.locator('#compliance-form [name="host_alias"]').fill("invalid:group")
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess" and request["remediation"] is None


def test_scap_datastream_routes_to_assessment_only(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="openscap"]').click()
    expect(page.locator("#compliance-custom")).to_be_hidden()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_disabled()
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_hidden()
    expect(page.locator("#compliance-openscap")).to_contain_text(
        "The approved scan checks SSH access"
    )
    path = "profiles/datastream.xml"
    settings = {
        "sha256": "a" * 64,
        "profile_id": "xccdf_example_profile_server",
        "distribution": "Ubuntu",
        "version": "24.04",
    }
    page.get_by_label("Datastream path").fill(path)
    page.get_by_role("button", name="Inspect datastream").click()
    inspection = last_request(page, "compliance.datastream.inspect")
    assert inspection["path"] == path
    assert not sent(page, "compliance.run")
    emit(
        page,
        "compliance.datastream.inspect",
        {
            "request_id": inspection["request_id"],
            "context_id": inspection["context_id"],
            "sha256": settings["sha256"],
            "profiles": [
                {"id": settings["profile_id"], "title": "Server baseline"},
                {
                    "id": "xccdf_example_profile_workstation",
                    "title": "Workstation baseline",
                },
            ],
            "ansible_available": True,
        },
    )
    expect(page.get_by_label("Datastream SHA-256")).to_have_value(settings["sha256"])
    expect(page.locator("#compliance-scap-status")).to_contain_text(
        "Ansible is available"
    )
    page.get_by_label("XCCDF profile", exact=True).select_option(
        label="Server baseline"
    )
    expect(page.locator("#compliance-scap-profile-id")).to_have_text(
        settings["profile_id"]
    )
    page.get_by_label("Target distribution", exact=True).fill(settings["distribution"])
    page.get_by_label("Target distribution version").fill(settings["version"])
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess"
    assert request["baseline_id"] is None
    assert request["remediation"] is None
    assert request["scan"]["profile"] == path
    assert request["scan"]["openscap"] == settings


def test_switching_hosts_reuses_only_the_inspected_benchmark(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="openscap"]').click()
    page.get_by_label("Datastream path").fill("profiles/datastream.xml")
    page.get_by_role("button", name="Inspect datastream").click()
    inspection = last_request(page, "compliance.datastream.inspect")
    profile = "xccdf_example_profile_server"
    emit(
        page,
        inspection["type"],
        {
            **inspection,
            "sha256": "a" * 64,
            "profiles": [{"id": profile, "title": "Server"}],
            "ansible_available": True,
        },
    )
    page.get_by_label("Target distribution", exact=True).fill("Ubuntu")
    page.get_by_label("Target distribution version").fill("24.04")
    entries.append(
        {
            **entries[0],
            "id": 9,
            "entry": {"kind": "domain", "value": "other.example.com"},
        }
    )
    snapshot(page, entries)
    page.locator("#compliance-host").select_option("9")
    expect(page.get_by_label("Datastream path")).to_have_value(
        "profiles/datastream.xml"
    )
    expect(page.get_by_label("Datastream SHA-256")).to_have_value("a" * 64)
    expect(page.get_by_label("XCCDF profile", exact=True)).to_have_value(profile)
    expect(page.get_by_label("Target distribution", exact=True)).to_have_value("")
    expect(page.get_by_label("Target distribution version")).to_have_value("")
    expect(page.locator('[name="private_key_file"]')).to_have_value("")
    assert len(sent(page, "compliance.datastream.inspect")) == 1
    assert not sent(page, "compliance.run")


def test_scap_path_edits_discard_stale_responses_and_clear_the_digest(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="openscap"]').click()
    path = page.get_by_label("Datastream path")
    inspect = page.get_by_role("button", name="Inspect datastream")
    profile = page.get_by_label("XCCDF profile", exact=True)
    digest = page.get_by_label("Datastream SHA-256")
    path.fill("old.xml")
    path.press("Enter")
    old = last_request(page, "compliance.datastream.inspect")
    path.fill("new.xml")
    inspect.click()
    new = last_request(page, "compliance.datastream.inspect")
    assert new["request_id"] != old["request_id"]
    result = {
        "context_id": new["context_id"],
        "sha256": "b" * 64,
        "profiles": [{"id": "xccdf_example_profile_server", "title": "Server"}],
        "ansible_available": True,
    }
    emit(page, new["type"], {**result, "request_id": old["request_id"]})
    expect(digest).to_have_value("")
    expect(profile).to_be_disabled()
    emit(page, new["type"], {**result, "request_id": new["request_id"]})
    expect(digest).to_have_value("b" * 64)
    expect(profile).to_have_value("xccdf_example_profile_server")
    expect(page.locator("#compliance-scap-status")).to_contain_text(
        "Ansible is available"
    )
    path.fill("another.xml")
    expect(digest).to_have_value("")
    expect(profile).to_be_disabled()
    page.locator('[data-compliance-action="assess"]').click()
    expect(page.locator("#compliance-status")).to_contain_text("Inspect the datastream")
    assert not sent(page, "compliance.run")


def test_missing_ansible_blocks_scanning_until_rechecked(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="openscap"]').click()
    page.get_by_label("Datastream path").fill("datastream.xml")
    page.get_by_role("button", name="Inspect datastream").click()
    inspection = last_request(page, "compliance.datastream.inspect")
    profile_id = "xccdf_example_profile_server"
    emit(
        page,
        inspection["type"],
        {
            **inspection,
            "sha256": "a" * 64,
            "profiles": [{"id": profile_id, "title": "Server"}],
            "ansible_available": False,
        },
    )
    page.get_by_label("Target distribution", exact=True).fill("Ubuntu")
    page.get_by_label("Target distribution version").fill("24.04")
    ready = page.locator("#compliance-settings-status")
    scan = page.get_by_role("button", name="Scan host", exact=True)
    recheck = page.get_by_role("button", name="Recheck Ansible")
    expect(ready).to_contain_text("Ansible is missing on the controller")
    expect(scan).to_be_disabled()
    recheck.click()
    check = last_request(page, "compliance.ansible.check")
    expect(recheck).to_be_disabled()
    emit(page, check["type"], {**check, "ansible_available": False})
    expect(scan).to_be_disabled()
    recheck.click()
    retry = last_request(page, "compliance.ansible.check")
    assert retry["request_id"] != check["request_id"]
    emit(page, retry["type"], {**retry, "ansible_available": True})
    expect(ready).to_contain_text("Ready to assess")
    expect(recheck).to_be_hidden()
    expect(page.get_by_label("XCCDF profile", exact=True)).to_have_value(profile_id)
    expect(page.get_by_label("Datastream SHA-256")).to_have_value("a" * 64)
    assert not sent(page, "compliance.run")
    scan.click()
    assert (
        last_request(page, "compliance.run")["workflow_request"]["scan"]["openscap"][
            "profile_id"
        ]
        == profile_id
    )


def test_saved_scap_reassessment_reuses_the_pinned_profile(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record["status"] = "attention"
    record["request"].update(action="assess", baseline_id=None)
    settings = {
        "distribution": "Ubuntu",
        "version": "22.04",
        "profile_id": "xccdf_example_profile_server",
        "sha256": "a" * 64,
    }
    record["request"]["scan"].update(
        profile="/workspace/benchmark.xml", openscap=settings
    )
    emit(page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]})
    page.locator("#compliance-reassess").click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["assessment_of"] == record["id"]
    assert request["scan"]["openscap"] == settings
    assert not sent(page, "compliance.datastream.inspect")


@pytest.mark.parametrize("width", [390, 1440])
def test_existing_project_identity_and_dependencies_are_visible(dashboard: Page, width):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page, configure=True)
    connect(page)
    card = page.locator('[data-compliance-baseline="custom"]')
    expect(card).to_contain_text("Existing InSpec project")
    card.click()
    page.locator("#compliance-custom summary").click()
    expect(page.locator("#compliance-custom")).to_contain_text(
        "does not require an Ansible playbook"
    )
    page.locator('#compliance-form [name="profile"]').fill("/profiles/upstream-project")
    page.locator('#compliance-form [name="inputs_file"]').fill("/profiles/inputs.yml")
    record = saved_preview(page)
    record.update(status="attention", label="upstream-project")
    record["request"]["action"] = "assess"
    record["request"]["remediation"] = None
    record["plan"].update(
        changes="Assessment only. No remediation requested for this project.",
        profile_bundle={
            "name": "upstream-project",
            "version": "2.0.0",
            "dependencies": [{"name": "shared"}],
        },
    )
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    expect(page.locator("#compliance-plan-summary")).to_contain_text(
        "upstream-project · version 2.0.0 · 1 bundled dependency"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"/tmp/kerkov-existing-project-{width}.png", full_page=True)


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_compliance_layout_and_disconnected_actions(dashboard: Page, width, theme):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_compliance(page, configure=True)
    headings = [
        page.locator(f"#{heading}").bounding_box()
        for heading in (
            "compliance-host",
            "compliance-baseline-heading",
            "compliance-launch-heading",
        )
    ]
    assert headings[0]["y"] < headings[1]["y"] < headings[2]["y"]
    assert page.locator(
        ".compliance-main-actions button:visible"
    ).all_text_contents() == [
        "Customize",
        "Scan host",
    ]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.evaluate("window.testSocket.close()")
    expect(page.locator('[data-compliance-action="assess"]')).to_be_disabled()
    expect(page.locator("#compliance-add-asset")).to_be_disabled()
    assert sent(page, "compliance.run") == []


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_scan_host_recovers_access_then_starts_assessment_with_one_click(
    dashboard: Page, tmp_path, width, theme
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_compliance(page)
    expect(page.locator("#compliance-baselines button:visible")).to_have_count(2)
    expect(page.locator("#compliance-projects button:visible")).to_have_count(2)
    expect(page.locator("#compliance-profile-search")).to_be_hidden()
    expect(page.locator('[name="private_key_file"]')).to_be_hidden()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_hidden()
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Scan host", exact=True)).to_be_visible()
    expect(page.locator("#compliance-profile-name")).to_have_text(
        "Linux account protection"
    )
    previous = last_request(page, "inventory.ssh_access")
    page.get_by_role("button", name="Scan host", exact=True).click()
    assert not sent(page, "compliance.run")
    emit_access(page, [], request=previous)
    request = last_request(page, "compliance.run")
    assert "workflow_request" not in request
    assert "use recall" in request["message"]
    assert "do not start/stop containers, scan, authenticate" in request["message"]
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    assert last_request(page, "inventory.ssh_access") != previous
    emit_access(page, [_create_mock_access()])
    expect(page.locator("#compliance-settings-status")).to_contain_text(
        "Ready to assess"
    )
    assert not page.locator("#compliance-advanced").evaluate("node => node.open")
    assert len(sent(page, "compliance.run")) == 2
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=str(tmp_path / f"clean-compliance-{width}.png"), full_page=True
    )
    assessment = last_request(page, "compliance.run")["workflow_request"]
    assert assessment["action"] == "assess"
    assert assessment["scan"]["user"] == "root"
    assert assessment["remediation"] is None


@pytest.mark.parametrize("stopped", [False, True])
def test_scan_host_stops_after_unsuccessful_access_lookup(dashboard: Page, stopped):
    page = dashboard
    open_compliance(page)
    emit_access(page, [])
    page.get_by_role("button", name="Scan host", exact=True).click()
    lookup = last_request(page, "compliance.run")
    assert "workflow_request" not in lookup
    emit(
        page,
        "user.message.complete",
        {"message_id": lookup["message_id"], "stopped": stopped},
    )
    emit_access(page, [_create_mock_access()] if stopped else [])
    expect(page.locator("#compliance-status")).to_contain_text(
        "Workflow stopped" if stopped else "No complete SSH access was recovered"
    )
    assert len(sent(page, "compliance.run")) == 1


def test_switching_hosts_cancels_a_pending_one_click_assessment(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page)
    lookup = last_request(page, "inventory.ssh_access")
    page.get_by_role("button", name="Scan host", exact=True).click()
    entries.append(
        {
            **entries[0],
            "id": 9,
            "entry": {"kind": "domain", "value": "other.example.com"},
        }
    )
    snapshot(page, entries)
    page.locator("#compliance-tab").click()
    page.locator("#compliance-host").select_option("9")
    emit_access(page, [_create_mock_access()], request=lookup)
    expect(page.locator("#compliance-host")).to_have_value("9")
    expect(page.locator('[name="user"]')).to_have_value("")
    assert not sent(page, "compliance.run")


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_customize_keeps_connection_edits_and_selected_profile(dashboard, width, theme):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_compliance(page)
    page.locator('[data-compliance-baseline="ssh-essentials"]').click()
    page.locator("#compliance-customize").click()
    expect(page.locator("#compliance-customize-heading")).to_be_focused()
    expect(page.locator("#compliance-customize")).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(page.locator('[name="private_key_file"]')).to_be_visible()
    connect(page)
    page.locator("#compliance-customize-done").click()
    expect(page.locator("#compliance-customize")).to_be_focused()
    expect(page.locator("#compliance-customize")).to_have_attribute(
        "aria-expanded", "false"
    )
    expect(page.locator('[name="private_key_file"]')).to_be_hidden()
    expect(
        page.locator('[data-compliance-baseline="ssh-essentials"]')
    ).to_have_attribute("aria-pressed", "true")
    page.locator("#compliance-customize").click()
    expect(page.locator('[name="private_key_file"]')).to_have_value("/keys/audit key")
    assert sent(page, "compliance.run") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_profile_filters_preserve_selection_and_connection(
    dashboard: Page, tmp_path, width, theme
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    open_compliance(page, configure=True)
    connect(page)
    selected = page.locator('[data-compliance-baseline][aria-pressed="true"]')
    baseline_id = selected.get_attribute("data-compliance-baseline")
    page.locator("#compliance-profile-source").select_option("local")
    expect(page.locator("[data-compliance-baseline]:visible")).to_have_count(2)
    expect(page.locator("#compliance-community-heading")).to_be_hidden()
    page.locator("#compliance-profile-search").fill("no matching profile")
    expect(page.locator("#compliance-profile-empty")).to_be_visible()
    page.locator("#compliance-profile-clear").click()
    expect(page.locator("#compliance-profile-search")).to_be_focused()
    expect(selected).to_have_attribute("data-compliance-baseline", baseline_id)
    expect(page.locator('[name="user"]')).to_have_value("audit")
    page.locator("#compliance-profile-source").select_option("community")
    expect(page.locator("[data-compliance-baseline]:visible")).to_have_count(0)
    expect(page.locator("[data-compliance-project]:visible")).not_to_have_count(0)
    page.locator("#compliance-profile-source").select_option("all")
    assert sent(page, "compliance.run") == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#compliance-view").evaluate("node => node.scrollTop = 0")
    page.screenshot(path=str(tmp_path / f"compliance-setup-{width}.png"))


def test_setup_status_opens_first_missing_assessment_setting(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    expect(page.locator("#compliance-settings-status")).to_contain_text(
        "Scan host looks up SSH access"
    )
    page.locator("#compliance-complete-settings").click()
    expect(page.locator('[name="user"]')).to_be_focused()
    connect_fields = {
        "user": "audit",
        "private_key_file": "/keys/audit",
        "known_hosts_file": "/keys/trusted",
    }
    for name, value in connect_fields.items():
        page.locator(f'[name="{name}"]').fill(value)
    expect(page.locator("#compliance-settings-status")).to_contain_text(
        "Ready to assess"
    )
    expect(page.locator("#compliance-complete-settings")).to_be_hidden()
    page.locator('[name="port"]').fill("0")
    expect(page.locator("#compliance-settings-status")).to_contain_text(
        "Review the missing details"
    )
    page.locator('[name="port"]').fill("22")
    page.locator('[data-compliance-baseline="custom"]').click()
    page.locator("#compliance-complete-settings").click()
    expect(page.locator('[name="profile"]')).to_be_focused()
    assert sent(page, "compliance.run") == []


@pytest.mark.parametrize("width", [1440, 390])
def test_assessment_progress_approvals_and_result_link_in_conversation(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record.update(status="running", stage="assessment")
    record["request"]["action"] = "assess"
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")
    assert request["context_id"]
    card = page.get_by_role("article", name="Compliance assessment")
    expect(card).to_contain_text("example.com:2222 · Linux account protection")
    expect(page.locator("#conversation-view")).to_be_visible()
    emit(
        page,
        "user.message.accepted",
        {"message_id": request["message_id"], "stoppable": True},
    )
    expect(page.get_by_role("button", name="Stop main run")).to_be_visible()

    def update(status, stage, detail):
        record.update(status=status, stage=stage, detail=detail)
        emit_socket(
            page,
            "compliance.updated",
            {
                "context_id": "context-1",
                "message_id": request["message_id"],
                "run": record,
            },
        )

    update("running", "assessment", "Readiness checks complete.")
    emit(page, "tool.start", {"name": "compliance_scan", "params": {}})
    expect(card).to_contain_text("Assessing baseline…")
    emit(
        page,
        "approval.request",
        {
            "approval_id": "workflow-approval",
            "tool": "compliance_scan",
            "params": {"host": "example.com"},
        },
    )
    expect(page.locator("#conversation-approval-host #approval-tray")).to_be_visible()
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
    expect(page.locator("#message-stream")).to_contain_text("reports/check.json")
    expect(page.locator("#compliance-workflow-output")).not_to_contain_text(
        "reports/check.json"
    )
    update("passed", "finished", "All selected checks passed.")
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    expect(card).to_contain_text("All selected checks passed.")
    expect(page.locator("#stop-button")).to_be_hidden()
    card.get_by_role("button", name="View assessment results").click()
    expect(page.locator("#compliance-view")).to_be_visible()
    expect(page.locator("#compliance-result-title")).to_have_text(
        "Baseline checks passed"
    )


def test_failed_assessment_offers_preview_in_conversation(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record["request"]["action"] = "assess"
    record.update(status="attention", before={"counts": {"passed": 3, "failed": 1}})
    page.locator('[data-compliance-action="assess"]').click()
    message_id = last_request(page, "compliance.run")["message_id"]
    emit_socket(
        page,
        "compliance.updated",
        {"context_id": "context-1", "message_id": message_id, "run": record},
    )
    emit(page, "user.message.complete", {"message_id": message_id})
    assessment = page.get_by_role("article", name="Compliance assessment")
    preview = assessment.get_by_role("button", name="Preview fixes")
    expect(preview).to_have_accessible_description(
        "Preview fixes uses Ansible check mode, which can still make changes. Tool approvals still apply."
    )
    preview.click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "preview"
    assert request["scan"] == record["request"]["scan"]
    expect(page.get_by_role("article", name="Compliance preview")).to_be_visible()


def test_disconnect_does_not_leave_a_workflow_running_or_claim_success(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
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


def test_custom_preview_keeps_original_profile_path_for_apply(dashboard: Page):
    from kerkov.core.compliance.workflows import WorkflowRequest

    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    page.locator('[data-compliance-baseline="custom"]').click()
    page.locator("#compliance-custom summary").click()
    for name, value in {
        "profile": "/profiles/upstream-project",
        "automation_profile": "stig_vm1_config",
        "playbook": "remediate",
        "host_alias": "compliance_vm1",
    }.items():
        page.locator(f'#compliance-form [name="{name}"]').fill(value)
    record = saved_preview(page)
    record["plan"].update(
        profile="/workspace/automation/compliance-runs/preview/profile",
        profile_bundle={
            "source": "/profiles/upstream-project",
            "dependencies": [{"name": "shared"}],
        },
    )
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    page.locator("#compliance-review-apply").click()
    page.locator("#compliance-tab").click()
    page.locator("#compliance-setup > summary").click()
    connect(page)
    page.locator('#compliance-form [name="port"]').fill("2223")
    page.locator("#conversation-tab").click()
    page.get_by_role("button", name="Apply fixes", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]

    assert request["scan"]["port"] == 2222
    assert request["scan"]["profile"] == record["request"]["scan"]["profile"]
    assert (
        WorkflowRequest.model_validate(request).signature()
        == WorkflowRequest.model_validate(record["request"]).signature()
    )


def test_apply_requires_a_reviewed_preview(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_hidden()
    page.locator("#compliance-form").evaluate("form => form.requestSubmit()")
    assert sent(page, "compliance.run") == []
    review_saved_preview(page)
    card = page.get_by_role("article", name="Compliance preview")
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(card).to_contain_text("Review the saved plan before applying.")
    card.get_by_role("button", name="Apply fixes").click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["preview_id"] == "a" * 32
    assert request["action"] == "apply"
    assert request["scan"]["host"] == "example.com"


def test_fix_actions_follow_failed_scan_and_selected_profile(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    preview = page.locator('[data-compliance-action="preview"]')
    apply = page.get_by_role("button", name="Apply fixes", exact=True)
    expect(preview).to_be_hidden()
    expect(apply).to_be_hidden()
    record = show_failed_assessment(page)
    expect(page.locator("#compliance-result-preview")).to_be_visible()
    expect(apply).to_be_hidden()
    page.locator("#compliance-setup > summary").click()
    expect(preview).to_be_visible()
    page.locator('[data-compliance-baseline="ssh-essentials"]').click()
    expect(preview).to_be_hidden()
    page.locator('[data-compliance-baseline="linux-accounts"]').click()
    expect(preview).to_be_visible()
    record.update(status="passed", before={"counts": {"passed": 4, "failed": 0}})
    emit_socket(page, "compliance.updated", {"context_id": "context-1", "run": record})
    expect(preview).to_be_hidden()
    expect(apply).to_be_hidden()
    expect(page.locator("#compliance-result-preview")).to_be_hidden()
    assert sent(page, "compliance.run") == []


def test_errored_scan_does_not_offer_remediation(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record.update(
        status="attention", before={"counts": {"passed": 0, "failed": 0, "error": 1}}
    )
    record["request"]["action"] = "assess"
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    expect(page.locator("#compliance-result-preview")).to_be_hidden()
    page.locator("#compliance-setup > summary").click()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_hidden()
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_hidden()


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
    open_compliance(page, configure=True)
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
    open_compliance(page, configure=True)
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
    page.locator("#compliance-run-details > summary").click()
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


@pytest.mark.parametrize("failure", ["busy", "validation"])
def test_failed_reassessment_does_not_attach_next_manual_assessment(
    dashboard: Page, failure
):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record["status"] = "interrupted"
    if failure == "validation":
        record["request"]["scan"]["user"] = ""
    else:
        page.evaluate(
            "window.originalStartWorkflow = inventoryPanel.startWorkflow; inventoryPanel.startWorkflow = () => null"
        )
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    page.locator("#compliance-reassess").click()
    assert not sent(page, "compliance.run")
    if failure == "busy":
        page.evaluate(
            "() => { inventoryPanel.startWorkflow = window.originalStartWorkflow; }"
        )
        page.locator("#compliance-tab").click()
        page.locator("#compliance-setup > summary").click()
    if failure == "validation":
        expect(page.locator("#compliance-setup")).to_have_attribute("open", "")
        page.locator('#compliance-form [name="user"]').fill("audit")
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["assessment_of"] is None


def test_reassessment_is_disabled_without_prepared_profile(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    record.update(status="failed", plan={})
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]}
    )
    expect(page.locator("#compliance-reassess")).to_be_disabled()


def test_python_installation_is_an_explicit_restored_option(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    option = page.locator('#compliance-form [name="bootstrap_python"]')
    expect(option).not_to_be_checked()
    option.check()
    review_saved_preview(page)
    page.get_by_role("button", name="Apply fixes", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["bootstrap_python"] is True


@pytest.mark.parametrize(
    "field", ["compliance-project-url", "compliance-profile-search"]
)
def test_profile_inputs_never_apply_fixes_on_enter(dashboard: Page, field):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    review_saved_preview(page)
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_enabled()
    page.locator("#compliance-tab").click()
    page.locator("#compliance-setup > summary").click()
    page.locator("#compliance-customize").click()
    if not page.locator("#compliance-profile-options").evaluate("node => node.open"):
        page.locator("#compliance-profile-options > summary").click()
    page.locator(f"#{field}").fill(
        "https://github.com/example/profile" if field.endswith("url") else "Linux"
    )
    page.locator(f"#{field}").press("Enter")
    assert sent(page, "compliance.run") == []


def test_customize_can_filter_and_close_without_a_host(dashboard: Page):
    page = dashboard
    snapshot(page, [])
    page.locator("#compliance-tab").click()
    page.locator("#compliance-customize").click()
    page.locator("#compliance-profile-options > summary").click()
    page.locator("#compliance-profile-search").fill("Linux")
    page.locator("#compliance-profile-source").select_option("builtin")
    expect(page.locator('[name="user"]')).to_be_disabled()
    page.locator("#compliance-customize-done").click()
    expect(page.locator("#compliance-customize")).to_be_focused()
    expect(page.locator("#compliance-customize")).to_have_attribute(
        "aria-expanded", "false"
    )


def test_done_clears_hidden_profile_filters(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    page.locator("#compliance-profile-search").fill("no matching profile")
    page.locator("#compliance-profile-source").select_option("local")
    expect(page.locator("#compliance-profile-empty")).to_be_visible()
    page.locator("#compliance-customize-done").click()
    expect(page.locator("#compliance-profile-empty")).to_be_hidden()
    expect(page.locator('[data-compliance-baseline="linux-accounts"]')).to_be_visible()
    expect(page.locator("#compliance-profile-search")).to_have_value("")
    expect(page.locator("#compliance-profile-source")).to_have_value("all")


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("has_saved_run", [False, True])
def test_completion_preserves_feedback_without_a_result_or_when_stopped(
    dashboard: Page, stopped, has_saved_run
):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    record = saved_preview(page)
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")
    if has_saved_run:
        emit_socket(
            page, "compliance.updated", {"context_id": "context-1", "run": record}
        )
    emit(
        page,
        "user.message.complete",
        {"message_id": request["message_id"], "stopped": stopped},
    )
    expected = (
        "Workflow stopped."
        if stopped
        else (
            ""
            if has_saved_run
            else "No saved assessment result was received. Check the activity log or refresh assessment history."
        )
    )
    expect(page.locator("#compliance-status")).to_have_text(expected)


def test_preview_check_mode_warning_is_visible_by_both_actions(dashboard: Page):
    page = dashboard
    open_compliance(page, configure=True)
    connect(page)
    show_failed_assessment(page)
    warning = page.get_by_text(
        "Preview fixes uses Ansible check mode, which can still make changes. "
        "Tool approvals still apply.",
        exact=True,
    )
    expect(page.locator("#compliance-result-preview")).to_be_visible()
    expect(warning.filter(visible=True)).to_have_count(1)
    page.locator("#compliance-setup > summary").click()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_visible()
    expect(
        page.locator("#compliance-form").get_by_text(
            "which can still make changes", exact=False
        )
    ).to_be_visible()


def test_adding_a_host_returns_to_compliance(dashboard: Page):
    page = dashboard
    entries, _ = open_compliance(page)
    page.get_by_role("button", name="+ Add host").click()
    expect(page.locator("#inventory-kind")).to_have_value("ip")
    page.locator("#inventory-value").fill("192.0.2.12")
    page.locator("#inventory-save").click()
    request = last_request(page, "inventory.save")
    emit_socket(
        page,
        "inventory.saved",
        {"context_id": "context-1", "request_id": request["request_id"], "entry_id": 9},
    )
    entries.append(
        {
            **entries[0],
            "id": 9,
            "section": "assets",
            "entry": {"kind": "ip", "value": "192.0.2.12"},
        }
    )
    emit_socket(
        page,
        "inventory.snapshot",
        {
            "context_id": "context-1",
            "request_id": request["request_id"],
            "available": True,
            "entries": entries,
        },
    )
    expect(page.locator("#compliance-view")).to_be_visible()
    expect(page.locator("#compliance-host")).to_have_value("9")


@pytest.mark.parametrize("width", [390, 1440])
def test_switching_hosts_from_results_opens_the_new_host_setup(dashboard: Page, width):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    entries, _ = open_compliance(page, configure=True)
    connect(page)
    original_run = show_failed_assessment(page)
    entries.append(
        {
            **entries[0],
            "id": 9,
            "entry": {
                "kind": "domain",
                "value": "other.example.com",
                "name": "Other host",
            },
        }
    )
    snapshot(page, entries)
    expect(page.locator("#compliance-result")).to_be_visible()
    expect(page.locator("#compliance-setup")).not_to_have_attribute("open", "")
    expect(page.locator("#compliance-host")).to_be_visible()
    page.locator("#compliance-host").select_option("9")
    expect(page.locator("#compliance-setup")).to_have_attribute("open", "")
    expect(page.locator("#compliance-result")).to_be_hidden()
    expect(page.locator('[name="user"]')).to_have_value("")
    expect(page.locator('[name="private_key_file"]')).to_have_value("")
    expect(page.locator("#compliance-selection")).to_contain_text("Other host")
    expect(page.locator("#compliance-host")).to_have_value("9")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    emit_socket(
        page, "compliance.snapshot", {"context_id": "context-1", "runs": [original_run]}
    )
    expect(page.locator("#compliance-result")).to_be_hidden()

    access_request = last_request(page, "inventory.ssh_access")
    assert access_request["entry_id"] == 9
    emit(
        page,
        "inventory.ssh_access",
        {
            **access_request,
            "resource": {"kind": "domain", "value": "other.example.com"},
            "candidates": [
                _create_mock_access(
                    port=22, user="other", private_key_file="/keys/other"
                )
            ],
        },
    )
    page.locator('[data-compliance-action="assess"]').click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["resource_id"] == 9
    assert request["scan"]["host"] == "other.example.com"
    assert request["scan"]["user"] == "other"
    assert request["scan"]["private_key_file"] == "/keys/other"
