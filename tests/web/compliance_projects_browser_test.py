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
    expect(page.locator("#compliance-projects button")).to_have_count(2)
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
