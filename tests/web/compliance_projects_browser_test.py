"""Catalog and URL preparation stay separate from target assessments."""

import sys

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.host_compliance_browser_test import connect, emit, open_compliance
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


def _open(page, cached=False):
    open_compliance(page)
    connect(page)
    _reply(page, "compliance.projects", projects=[_ready()] if cached else [])
    page.locator('[data-compliance-project="devsec-linux"]').click()


@pytest.mark.parametrize("width", [390, 1440])
def test_catalog_first_use_validates_then_prepares_before_assessment(
    dashboard: Page, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    _open(page)
    expect(page.locator("#compliance-projects button")).to_have_count(2)
    expect(page.locator('[data-compliance-action="assess"]')).to_be_disabled()
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
    _reply(
        page,
        "compliance.project.validate",
        validated={
            "url": URL,
            "name": "linux-baseline",
            "version": "2.11.0",
            "commit": COMMIT,
            "dependencies": [],
        },
    )
    expect(page.locator("#compliance-project-prepare")).to_be_enabled()
    page.locator("#compliance-project-prepare").click()
    request = last_request(page, "compliance.run")
    assert request.get("workflow_request") is None
    assert (
        "compliance_profile" in request["message"]
        and "refresh=false" in request["message"]
    )
    assert URL in request["message"]
    assert f'expected_commit="{COMMIT}"' in request["message"]
    emit(
        page,
        "tool.result",
        {
            "name": "compliance_profile",
            "success": True,
            "output": "Ready",
            "metadata": _ready(),
        },
    )
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-status")).to_contain_text("reused until")
    expect(page.locator('[data-compliance-action="assess"]')).to_be_enabled()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_disabled()
    expect(page.locator('#compliance-form button[type="submit"]')).to_be_disabled()
    expect(page.locator("#compliance-local-profile")).to_be_hidden()
    expect(page.locator('#compliance-form [name="bootstrap_python"]')).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"/tmp/kerkov-url-catalog-{width}.png", full_page=True)
    page.locator('[data-compliance-action="assess"]').click()
    workflow = last_request(page, "compliance.run")["workflow_request"]
    assert workflow["action"] == "assess"
    assert workflow["scan"]["profile"] == _ready()["profile"]
    assert workflow["remediation"] is None
    assert workflow["bootstrap_python"] is False


def test_cached_catalog_selection_checks_local_copy_without_url_validation(
    dashboard: Page,
):
    page = dashboard
    _open(page, cached=True)
    assert sent(page, "compliance.project.validate") == []
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-refresh")).to_be_enabled()
    expect(page.locator("#compliance-project-prepare")).to_be_disabled()
    assert sent(page, "compliance.run") == []


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
    assert sent(page, "compliance.run") == []


def test_failed_refresh_keeps_previous_ready_project_and_requires_explicit_click(
    dashboard: Page,
):
    page = dashboard
    _open(page, cached=True)
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    refresh = page.locator("#compliance-project-refresh")
    refresh.click()
    assert sent(page, "compliance.run") == []
    _reply(
        page,
        "compliance.project.validate",
        validated={
            "url": URL,
            "name": "linux-baseline",
            "version": "2.12.0",
            "commit": COMMIT,
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
    expect(refresh).to_have_text("Prepare refresh")
    refresh.click()
    request = last_request(page, "compliance.run")
    assert "refresh=true" in request["message"]
    emit(
        page,
        "tool.result",
        {
            "name": "compliance_profile",
            "success": False,
            "error": "Preparation failed",
            "output": "",
        },
    )
    emit(page, "user.message.complete", {"message_id": request["message_id"]})
    _reply(page, "compliance.project.cached", url=URL, project=_ready())
    expect(page.locator("#compliance-project-status")).to_contain_text(
        "previous ready copy was kept"
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
