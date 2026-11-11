"""Control investigation, stale-response isolation and report downloads."""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from tests.web.browser_support import emit, last_request
from tests.web.host_compliance_browser_test import (
    connect,
    open_compliance,
    saved_preview,
)


def _show_record(page):
    open_compliance(page)
    connect(page)
    record = saved_preview(page)
    record["controls"].extend(
        {
            "profile": "accounts",
            "control_id": f"control-{status}",
            "title": f"Check {status}",
            "impact": 0.5,
            "before": status,
            "after": "unassessed",
        }
        for status in ("passed", "error", "skipped", "waived", "no_tests")
    )
    emit(page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]})
    return record


def _reply_control(page, request, **updates):
    emit(
        page,
        "compliance.control",
        {
            **request,
            "control": {
                "notice": "Local evidence may contain sensitive configuration.",
                "remediation": "Set /etc/passwd to root:root and mode 0644.",
                "before": {
                    "status": "failed",
                    "description": "Protect account files",
                    "tests": [
                        {
                            "status": "failed",
                            "code_desc": "File /etc/passwd mode is expected to eq 0644",
                            "message": '<img src=x onerror="window.evidenceExecuted=true"> observed 0666',
                        }
                    ],
                    "test_count": 1,
                    "report_artifact": "/workspace/artifacts/compliance/scan/source.json",
                    "report_sha256": "a" * 64,
                },
                "after": {"status": "unassessed", "tests": []},
            },
            **updates,
        },
    )


@pytest.mark.parametrize("width,theme", [(1440, "dark"), (390, "light")])
def test_control_drawer_shows_evidence_safely_and_restores_keyboard_focus(
    dashboard: Page, tmp_path, width, theme
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
    _show_record(page)
    button = page.get_by_role("button", name="Protect account file", exact=True)
    button.click()
    request = last_request(page, "compliance.control")
    assert request["profile"] == "accounts"
    _reply_control(page, request)
    drawer = page.locator("#compliance-control-drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("expected to eq 0644")
    expect(drawer).to_contain_text("observed 0666")
    expect(drawer).to_contain_text("After: unassessed")
    expect(drawer).to_contain_text("Set /etc/passwd to root:root")
    assert drawer.locator("img").count() == 0
    assert page.evaluate("window.evidenceExecuted") is None
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert drawer.evaluate("node => node.scrollWidth <= node.clientWidth")
    page.screenshot(path=str(tmp_path / f"compliance-control-{width}.png"))
    page.keyboard.press("Escape")
    expect(drawer).not_to_be_visible()
    expect(button).to_be_focused()
    assert page.locator("#compliance-control-detail").text_content() == ""


def test_filters_use_latest_assessed_outcome_and_search_profile_title_or_id(dashboard):
    page = dashboard
    record = _show_record(page)
    filter = page.locator("#compliance-control-filter")
    filter.select_option("unresolved")
    expect(page.locator("#compliance-controls tr")).to_have_count(5)
    filter.select_option("error")
    expect(page.locator("#compliance-controls")).to_contain_text("Check error")
    expect(page.locator("#compliance-controls tr")).to_have_count(1)
    filter.select_option("all")
    page.locator("#compliance-control-search").fill("protect account")
    expect(page.locator("#compliance-controls tr")).to_have_count(1)
    page.locator("#compliance-control-search").fill("missing")
    expect(page.locator("#compliance-filter-notice")).to_have_text(
        "No loaded controls match these filters."
    )
    record["controls"][0]["after"] = "passed"
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    page.locator("#compliance-control-search").fill("")
    filter.select_option("unresolved")
    expect(page.locator("#compliance-controls tr")).to_have_count(4)


def test_late_control_reply_cannot_reopen_closed_drawer_or_cross_runs(dashboard):
    page = dashboard
    record = _show_record(page)
    page.get_by_role("button", name="Protect account file", exact=True).click()
    first = last_request(page, "compliance.control")
    page.keyboard.press("Escape")
    _reply_control(page, first)
    expect(page.locator("#compliance-control-drawer")).not_to_be_visible()
    page.get_by_role("button", name="Protect account file", exact=True).click()
    second = last_request(page, "compliance.control")
    record["id"] = "b" * 32
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    _reply_control(page, second)
    expect(page.locator("#compliance-control-drawer")).not_to_be_visible()
    assert page.locator("#compliance-control-detail").text_content() == ""


@pytest.mark.parametrize(
    "format,mime", [("json", "application/json"), ("html", "text/html")]
)
def test_report_downloads_use_server_content_and_show_errors(dashboard, format, mime):
    page = dashboard
    record = _show_record(page)
    if format == "json":
        page.locator("#compliance-run-details > summary").click()
    page.locator(f"#compliance-export-{format}").click()
    request = last_request(page, "compliance.export")
    assert request["format"] == format
    expect(page.locator("#compliance-export-html")).to_be_disabled()
    body = (
        json.dumps({"controls": [], "status": "attention"})
        if format == "json"
        else "<!doctype html><h1>Assessment</h1>"
    )
    with page.expect_download() as downloaded:
        emit(
            page,
            "compliance.export",
            {
                **request,
                "body": body,
                "mime": mime,
                "filename": f"kerkov-compliance-{record['id']}.{format}",
            },
        )
    download = downloaded.value
    assert download.suggested_filename.endswith(f".{format}")
    assert download.path().read_text() == body
    expect(page.locator("#compliance-export-html")).to_be_enabled()
    page.locator(f"#compliance-export-{format}").click()
    request = last_request(page, "compliance.export")
    emit(
        page,
        "compliance.export",
        {**request, "detail": "Evidence no longer matches its saved SHA-256"},
    )
    expect(page.locator("#compliance-export-status")).to_have_text(
        "Evidence no longer matches its saved SHA-256"
    )


def test_pending_download_is_discarded_on_disconnect(dashboard):
    page = dashboard
    _show_record(page)
    page.locator("#compliance-run-details > summary").click()
    page.locator("#compliance-export-json").click()
    request = last_request(page, "compliance.export")
    page.evaluate("window.testSocket.close()")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    emit(
        page,
        "compliance.export",
        {**request, "body": "{}", "mime": "application/json", "filename": "stale.json"},
    )
    expect(page.locator("#compliance-export-json")).to_be_disabled()
    assert downloads == []


@pytest.mark.parametrize("width", [390, 1440])
def test_completed_assessment_prioritizes_counts_and_failures_with_editable_setup(
    dashboard: Page, tmp_path, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_compliance(page)
    connect(page)
    record = saved_preview(page)
    record.update(status="attention", label="xccdf_example_profile_standard")
    record["request"].update(action="assess", baseline_id=None)
    record["request"]["scan"].update(
        profile="/workspace/benchmark.xml",
        openscap={
            "distribution": "Ubuntu",
            "version": "22.04",
            "profile_id": "xccdf_example_profile_standard",
            "sha256": "a" * 64,
        },
    )
    record["after"] = {
        "counts": {
            "passed": 23,
            "failed": 17,
            "error": 0,
            "skipped": 599,
            "waived": 0,
            "no_tests": 0,
        },
        "xccdf_counts": {
            "pass": 23,
            "fail": 17,
            "notapplicable": 5,
            "notselected": 594,
        },
        "controls_truncated": True,
    }
    record["controls"][0].update(
        control_id="xccdf_example_rule_passwd_permissions", after="failed"
    )
    record["controls"].append(
        {
            "profile": "accounts",
            "control_id": "passed-check",
            "title": "A passing check",
            "before": "unassessed",
            "after": "passed",
        }
    )
    emit(page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]})
    assert not page.locator("#compliance-setup").evaluate("node => node.open")
    assert not page.locator("#compliance-run-details").evaluate("node => node.open")
    assert not page.locator("#compliance-history-details").evaluate("node => node.open")
    expect(page.locator("#compliance-setup-profile")).to_contain_text(
        "Ubuntu 22.04 / Standard"
    )
    assert page.locator("#compliance-metrics strong").all_text_contents() == [
        "40",
        "23",
        "17",
        "0",
    ]
    expect(page.locator("#compliance-coverage-note")).to_have_text(
        "5 not applicable · 594 outside this profile"
    )
    expect(page.locator("#compliance-control-filter")).to_have_value("failed")
    expect(page.locator("#compliance-controls tr")).to_have_count(1)
    assert page.locator(".compliance-controls th:visible").all_text_contents() == [
        "Check",
        "Result",
    ]
    expect(page.locator("#compliance-controls")).not_to_contain_text("xccdf_example")
    button = page.get_by_role("button", name="Protect account file", exact=True)
    button.click()
    request = last_request(page, "compliance.control")
    assert request["control_id"] == "xccdf_example_rule_passwd_permissions"
    _reply_control(page, request)
    expect(page.locator("#compliance-control-drawer")).to_contain_text("Result: failed")
    expect(page.locator("#compliance-control-drawer")).not_to_contain_text(
        "After: unassessed"
    )
    expect(page.locator("#compliance-control-profile")).to_contain_text(
        request["control_id"]
    )
    page.keyboard.press("Escape")
    page.locator("#compliance-control-filter").select_option("all")
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    expect(page.locator("#compliance-control-filter")).to_have_value("all")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(
        path=str(tmp_path / f"compact-compliance-{width}.png"), full_page=True
    )
    page.locator("#compliance-setup > summary").click()
    expect(page.locator('[name="scap_datastream"]')).to_have_value(
        "/workspace/benchmark.xml"
    )
    expect(
        page.get_by_role("button", name="Run assessment", exact=True)
    ).to_be_visible()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_hidden()
    expect(page.locator('#compliance-form button[type="submit"]')).to_be_hidden()
    assert not page.evaluate(
        "window.testSocket.sent.some(message => message.type === 'compliance.run')"
    )
