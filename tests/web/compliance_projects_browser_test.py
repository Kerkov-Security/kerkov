"""Catalog and URL preparation stay separate from target assessments."""

import sys

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.host_compliance_browser_test import (
    _create_mock_access,
    connect,
    emit,
    emit_access,
    open_compliance,
    show_failed_assessment,
)
from tests.web.resource_profile_browser_test import emit as emit_socket
from tests.web.resource_profile_browser_test import last_request, sent, snapshot

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX compliance fixtures"
)
URL = "https://github.com/dev-sec/linux-baseline"
COMMIT = "a" * 40


def _ready(**updates):
    return {
        "url": URL,
        "commit": COMMIT,
        "name": "linux-baseline",
        "version": "2.11.0",
        "profile": "/cache/version-one/profile",
        "prepared_at": "2026-11-08T00:00:00Z",
        "dependencies": [],
        **updates,
    }


def _reply(page, type, **payload):
    request = last_request(page, type)
    emit_socket(
        page,
        type,
        {"context_id": "context-1", "request_id": request["request_id"], **payload},
    )


def _open(page, cached=False, configure=True):
    open_compliance(page, configure=configure)
    if configure:
        connect(page)
    else:
        emit_access(page, [_create_mock_access()])
    _reply(page, "compliance.projects", projects=[_ready()] if cached else [])
    page.locator('[data-compliance-project="devsec-linux"]').click()


def _prepare_from_scan(page):
    page.get_by_role("button", name="Scan host", exact=True).click()
    assert sent(page, "compliance.project.prepare") == []
    _reply(page, "compliance.project.validate", validated=_ready(ref="main"))
    return last_request(page, "compliance.project.prepare")


def _preparation_result(page, request, *, success=True, error=None, stopped=False):
    emit_socket(
        page,
        "compliance.event",
        {
            "message_id": request["message_id"],
            "event_type": "tool.result",
            "payload": {
                "name": "compliance_profile",
                "success": success,
                "error": error,
            },
        },
    )
    emit(
        page,
        "user.message.complete",
        {"message_id": request["message_id"], "stopped": stopped},
    )


@pytest.mark.parametrize("width", [390, 1440])
def test_catalog_first_use_validates_then_prepares_before_assessment(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    _open(page, configure=False)
    expect(page.locator('[data-compliance-project="devsec-linux"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#compliance-profile-name")).to_have_text("Linux hardening")
    expect(page.locator("#compliance-customize")).to_have_attribute(
        "aria-expanded", "false"
    )
    request = _prepare_from_scan(page)
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(page.get_by_role("article", name="Compliance assessment")).to_contain_text(
        "Preparing the selected profile"
    )
    expect(page.locator("#compliance-project-detail")).to_contain_text(COMMIT)
    assert sent(page, "compliance.run") == []
    assert request["project_request"] == {
        "url": URL,
        "refresh": False,
        "commit": COMMIT,
    }
    assert "message" not in request
    emit(
        page,
        "approval.request",
        {
            "approval_id": "prepare",
            "tool": "compliance_profile",
            "params": {
                "action": "prepare",
                "url": URL,
                "refresh": False,
                "expected_commit": COMMIT,
            },
        },
    )
    expect(
        page.locator('[data-approval-id="prepare"] .activity-detail')
    ).to_contain_text(COMMIT)
    expect(
        page.locator('[data-approval-id="prepare"] .activity-detail')
    ).to_contain_text(URL)
    emit(
        page,
        "approval.resolved",
        {"approval_id": "prepare", "decision": "approve_once"},
    )
    _preparation_result(page, request)
    assert sent(page, "compliance.run") == []
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-status")).to_contain_text("reused until")
    expect(page.locator('[data-compliance-action="preview"]')).to_be_disabled()
    expect(page.get_by_role("button", name="Apply fixes", exact=True)).to_be_hidden()
    expect(page.locator("#compliance-local-profile")).to_be_hidden()
    expect(page.locator('#compliance-form [name="bootstrap_python"]')).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"/tmp/kerkov-url-catalog-{width}.png", full_page=True)
    workflow = last_request(page, "compliance.run")["workflow_request"]
    assert workflow["action"] == "assess"
    assert workflow["scan"]["profile"] == _ready()["profile"]
    assert workflow["remediation"] is None
    assert workflow["bootstrap_python"] is False


@pytest.mark.parametrize(
    "width, query, title, url",
    [
        (
            390,
            "24.04 ubuntu",
            "Ubuntu 24.04 STIG",
            "https://github.com/mitre/canonical-ubuntu-24.04-lts-stig-baseline",
        ),
        (
            1440,
            "red hat 9 stig",
            "RHEL 9 STIG",
            "https://github.com/mitre/redhat-enterprise-linux-9-stig-baseline",
        ),
    ],
)
def test_search_finds_the_platform_and_prepares_the_selected_benchmark(
    dashboard: Page, width, query, title, url
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    emit_access(page, [_create_mock_access()])
    _reply(page, "compliance.projects", projects=[])
    page.get_by_role("searchbox", name="Search profiles").fill(query)
    match = page.locator("#compliance-projects button:visible")
    expect(match).to_have_count(1)
    expect(match).to_contain_text(title)
    match.click()
    expect(page.locator("#compliance-profile-name")).to_have_text(title)
    expect(page.locator("#compliance-advanced")).to_be_hidden()
    page.get_by_role("button", name="Scan host", exact=True).click()
    assert last_request(page, "compliance.project.validate")["url"] == url
    assert sent(page, "compliance.run") == []
    _reply(page, "compliance.project.validate", validated=_ready(url=url, ref="main"))
    assert last_request(page, "compliance.project.prepare")["project_request"] == {
        "url": url,
        "commit": COMMIT,
        "refresh": False,
    }


@pytest.mark.parametrize("width, major", [(390, 9), (1440, 10)])
def test_pci_profile_launches_openscap_without_manual_datastream_fields(
    dashboard: Page, width, major
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    emit_access(page, [_create_mock_access()])
    page.get_by_role("searchbox", name="Search profiles").fill(f"pci dss rhel {major}")
    match = page.locator("#compliance-projects button:visible")
    expect(match).to_have_count(1)
    expect(match).to_contain_text(f"PCI DSS v4.0.1 · RHEL {major}")
    match.click()
    expect(page.locator("#compliance-project-status")).to_contain_text(
        "pinned SCAP release"
    )
    expect(page.locator("#compliance-openscap")).to_be_hidden()
    expect(page.locator("#compliance-advanced")).to_be_hidden()
    page.get_by_role("button", name="Scan host", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess"
    assert request["scap_preset_id"].startswith(f"pci-dss-rhel{major}-")
    assert request["scan"]["profile"] == f"ssg-rhel{major}-ds.xml"
    assert request["scan"]["openscap"]["distribution"] == "RedHat"
    assert request["scan"]["openscap"]["version"] == f"{major}.*"
    assert request["scan"]["openscap"]["profile_id"].endswith("_pci-dss")
    assert request["remediation"] is None
    assert sent(page, "compliance.project.prepare") == []
    expect(page.get_by_role("article", name="Compliance assessment")).to_contain_text(
        f"PCI DSS v4.0.1 · RHEL {major}"
    )


def test_build_save_and_scan_custom_profile(dashboard: Page):
    page = dashboard
    open_compliance(page)
    emit_access(page, [_create_mock_access()])
    _reply(page, "compliance.projects", projects=[], custom_profiles=[])
    page.get_by_role("button", name="New profile").click()
    dialog = page.get_by_role("dialog", name="New profile")
    dialog.get_by_label("Profile name").fill("Web host checks")
    dialog.get_by_label("Check type", exact=True).select_option("file_mode")
    dialog.get_by_label("File path").fill("/etc/passwd")
    dialog.get_by_label("Octal mode").fill("0644")
    dialog.get_by_role("button", name="Add check").click()
    dialog.get_by_label("Check type", exact=True).nth(1).select_option(
        "package_installed"
    )
    dialog.get_by_label("Package name").fill("auditd")
    dialog.get_by_role("button", name="Save profile").click()
    draft = last_request(page, "compliance.custom.save")["draft"]
    assert draft == {
        "reference": None,
        "profile": {
            "name": "Web host checks",
            "checks": [
                {"kind": "file_mode", "target": "/etc/passwd", "expected": "0644"},
                {"kind": "package_installed", "target": "auditd", "expected": ""},
            ],
        },
    }
    entry = {**draft["profile"], "id": "c" * 32, "revision": "d" * 64}
    _reply(page, "compliance.custom.save", profile=entry)
    expect(page.get_by_role("group", name="My profiles")).to_be_visible()
    card = page.get_by_role("button", name="Web host checks 2 Linux checks")
    expect(card).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Edit Web host checks").click()
    expect(
        page.get_by_role("dialog", name="Edit profile").get_by_label("File path")
    ).to_have_value("/etc/passwd")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="Scan host", exact=True).click()
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess"
    assert request["custom_profile"] == {
        "id": entry["id"],
        "revision": entry["revision"],
    }
    assert request["scan"]["profile"] == entry["id"]
    assert request["remediation"] is None


def test_auto_recommends_a_verified_platform_and_waits_for_review(dashboard: Page):
    page = dashboard
    open_compliance(page)
    emit_access(page, [_create_mock_access()])
    _reply(page, "compliance.projects", projects=[])
    page.get_by_role("button", name="Auto assessment", exact=True).click()
    page.get_by_label("Assessment goal").select_option("stig")
    page.get_by_role("button", name="Scan host", exact=True).click()
    detection = last_request(page, "compliance.run")
    assert detection["workflow_request"]["action"] == "recommend"
    assert detection["workflow_request"]["goal"] == "stig"
    assert detection["workflow_request"]["scan"]["port"] == 2222
    emit_socket(
        page,
        "compliance.event",
        {
            "message_id": detection["message_id"],
            "event_type": "compliance.recommended",
            "payload": {
                "id": "mitre-ubuntu-2404-stig",
                "platform": "Ubuntu 24.04",
                "reason": "Verified Ubuntu 24.04 over SSH.",
            },
        },
    )
    emit(page, "user.message.complete", {"message_id": detection["message_id"]})
    recommendation = page.get_by_role("article", name="Compliance assessment")
    expect(recommendation).to_contain_text("Recommended profile · Ubuntu 24.04 STIG")
    assert not sent(page, "compliance.project.validate")
    recommendation.get_by_role("button", name="Scan with recommended profile").click()
    target = "https://github.com/mitre/canonical-ubuntu-24.04-lts-stig-baseline"
    assert last_request(page, "compliance.project.validate")["url"] == target
    _reply(
        page, "compliance.project.validate", validated=_ready(url=target, ref="main")
    )
    prepare = last_request(page, "compliance.project.prepare")
    _preparation_result(page, prepare)
    _reply(page, "compliance.project.cached", url=target, project=_ready(url=target))
    request = last_request(page, "compliance.run")["workflow_request"]
    assert request["action"] == "assess"
    assert request["scan"]["port"] == 2222
    assert request["remediation"] is None


@pytest.mark.parametrize("outcome", ["denied", "stopped", "replaced"])
def test_community_scan_stops_when_approved_preparation_cannot_be_used(
    dashboard, outcome
):
    page = dashboard
    _open(page, configure=False)
    request = _prepare_from_scan(page)
    _preparation_result(
        page,
        request,
        success=outcome == "replaced",
        error="Project preparation was denied." if outcome == "denied" else None,
        stopped=outcome == "stopped",
    )
    _reply(page, "compliance.project.cached", url=URL, project=_ready(commit="b" * 40))
    assert sent(page, "compliance.run") == []
    expected = {
        "denied": "denied",
        "stopped": "stopped",
        "replaced": "revision changed",
    }
    expect(page.locator("#compliance-status")).to_contain_text(expected[outcome])


def test_community_scan_lookup_and_preparation_continue_in_order(dashboard):
    page = dashboard
    open_compliance(page)
    _reply(page, "compliance.projects", projects=[])
    page.locator('[data-compliance-project="devsec-linux"]').click()
    old_access = last_request(page, "inventory.ssh_access")
    page.get_by_role("button", name="Scan host", exact=True).click()
    emit_access(page, [], request=old_access)
    discovery = last_request(page, "compliance.run")
    assert "workflow_request" not in discovery
    _reply(page, "compliance.project.validate", validated=_ready(ref="main"))
    assert not sent(page, "compliance.project.prepare")
    emit(page, "user.message.complete", {"message_id": discovery["message_id"]})
    emit_access(page, [_create_mock_access()])
    preparation = last_request(page, "compliance.project.prepare")
    _preparation_result(page, preparation)
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    runs = sent(page, "compliance.run")
    assert len(runs) == 2
    scan = runs[-1]["workflow_request"]["scan"]
    assert scan["profile"] == _ready()["profile"]
    assert scan["user"] == "root"


def test_community_scan_uses_verified_cache_without_repreparing(dashboard):
    page = dashboard
    _open(page, cached=True, configure=False)
    page.get_by_role("button", name="Scan host", exact=True).click()
    assert sent(page, "compliance.run") == []
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    scan = last_request(page, "compliance.run")["workflow_request"]["scan"]
    assert scan["profile"] == _ready()["profile"]
    assert not sent(page, "compliance.project.validate")
    assert not sent(page, "compliance.project.prepare")


def test_cached_catalog_selection_checks_local_copy_without_url_validation(
    dashboard: Page,
):
    page = dashboard
    _open(page, cached=True)
    assert sent(page, "compliance.project.validate") == []
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-refresh")).to_be_enabled()
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
    assert sent(page, "compliance.project.prepare") == []
    page.locator("#compliance-project-refresh").click()
    _reply(page, "compliance.project.validate", validated=_ready(ref="main"))
    expect(page.locator("#compliance-project-status")).to_contain_text(
        "Up to date with main (aaaaaaa)"
    )
    expect(page.locator("#compliance-project-update")).to_be_hidden()


def test_invalid_custom_url_and_stale_validation_cannot_enable_preparation(
    dashboard: Page,
):
    page = dashboard
    _open(page)
    old = last_request(page, "compliance.project.validate")
    page.locator("#compliance-project-url").fill("https://example.com/profile")
    emit_socket(
        page,
        "compliance.project.validate",
        {
            "context_id": "context-1",
            "request_id": old["request_id"],
            "validated": {"url": URL, "name": "stale", "version": "1"},
        },
    )
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
    page.locator("#compliance-project-validate").click()
    _reply(
        page,
        "compliance.project.validate",
        detail="Use an https://github.com repository URL",
    )
    expect(page.locator("#compliance-project-status")).to_contain_text("github.com")
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
    assert sent(page, "compliance.project.prepare") == []


def test_failed_refresh_keeps_previous_ready_project_and_requires_explicit_click(
    dashboard: Page,
):
    page = dashboard
    _open(page, cached=True)
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    refresh = page.locator("#compliance-project-refresh")
    refresh.click()
    assert sent(page, "compliance.project.prepare") == []
    _reply(
        page,
        "compliance.project.validate",
        validated={
            "url": URL,
            "name": "linux-baseline",
            "version": "2.12.0",
            "commit": "c" * 40,
            "dependencies": [
                {
                    "name": "shared",
                    "git": "https://github.com/example/shared",
                    "ref": "b" * 40,
                    "dependencies": [],
                }
            ],
        },
    )
    expect(page.locator("#compliance-project-detail")).to_contain_text(
        "https://github.com/example/shared @ " + "b" * 40
    )
    expect(page.locator("#compliance-project-detail")).to_contain_text(
        "Installed · v2.11.0 · aaaaaaa"
    )
    update = page.get_by_role("button", name="Update to ccccccc")
    expect(update).to_be_visible()
    expect(refresh).to_be_hidden()
    update.click()
    request = last_request(page, "compliance.project.prepare")
    assert request["project_request"] == {
        "url": URL,
        "refresh": True,
        "commit": "c" * 40,
    }
    emit(
        page,
        "compliance.event",
        {
            "message_id": request["message_id"],
            "event_type": "tool.result",
            "payload": {
                "name": "compliance_profile",
                "success": False,
                "error": "Preparation failed.",
            },
        },
    )
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-status")).to_contain_text(
        "Preparation failed. The ready copy shown below is available."
    )
    expect(page.locator('[data-compliance-action="assess"]')).to_be_enabled()
    assert (
        page.locator('#compliance-form [name="profile"]').input_value()
        == _ready()["profile"]
    )


def test_project_responses_are_discarded_after_engagement_change(dashboard: Page):
    page = dashboard
    _open(page)
    old = last_request(page, "compliance.project.validate")
    snapshot(page, [], context_id="new-engagement")
    emit_socket(
        page,
        "compliance.project.validate",
        {
            "context_id": "context-1",
            "request_id": old["request_id"],
            "validated": {"url": URL, "name": "old-project", "version": "1"},
        },
    )
    expect(page.locator("#compliance-project-detail")).not_to_contain_text(
        "old-project"
    )
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
