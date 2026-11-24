"""Control investigation, stale-response isolation and report downloads."""

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect

from kerkov.core.compliance.review import ControlQuery, browse_controls
from tests.core.compliance.review_test import _create_mock_large_record
from tests.web.browser_support import emit, last_request
from tests.web.host_compliance_browser_test import (
    connect,
    open_compliance,
    saved_preview,
)


def _show_record(page):
    open_compliance(page, configure=True)
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
                    "rationale": "Account files must remain protected.",
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
    expect(drawer).to_contain_text("Rationale: Account files must remain protected.")
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


def test_result_counts_filter_checks_and_clear_restores_all_loaded_controls(dashboard):
    page = dashboard
    record = _show_record(page)
    record["before"] = {"counts": {"passed": 1, "failed": 1, "error": 1, "skipped": 1}}
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    page.locator("#compliance-control-search").fill("no match")
    page.get_by_role("button", name="Show passed checks", exact=True).click()
    expect(page.locator("#compliance-control-search")).to_have_value("")
    expect(page.locator("#compliance-control-filter")).to_have_value("passed")
    expect(page.locator("#compliance-controls tr")).to_have_count(1)
    expect(page.get_by_role("button", name="Show passed checks")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("button", name="Show errored checks", exact=True).click()
    expect(page.locator("#compliance-controls")).to_contain_text("Check error")
    page.locator("#compliance-controls-clear").click()
    expect(page.locator("#compliance-controls tr")).to_have_count(6)
    expect(page.locator("#compliance-controls-clear")).to_be_hidden()
    expect(page.locator("#compliance-control-search")).to_be_focused()
    assert not page.locator('#compliance-metrics [aria-pressed="true"]').count()


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
    open_compliance(page, configure=True)
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
        "Impact",
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
    expect(page.get_by_role("button", name="Scan host", exact=True)).to_be_visible()
    expect(page.locator('[data-compliance-action="preview"]')).to_be_hidden()
    expect(page.locator('#compliance-form button[type="submit"]')).to_be_hidden()
    assert not page.evaluate(
        "window.testSocket.sent.some(message => message.type === 'compliance.run')"
    )


def _show_complete_record(page, tmp_path):
    open_compliance(page, configure=True)
    connect(page)
    record = _create_mock_large_record(tmp_path)
    record["request"]["scan"]["host"] = "example.com"
    record["label"] = "Linux security assessment"
    record["controls"] = record["before"]["controls"]

    def respond(request):
        try:
            return {
                **request,
                **browse_controls(
                    tmp_path, record, ControlQuery.model_validate(request["query"])
                ),
            }
        except ValueError as exc:
            return {**request, "detail": str(exc)}

    page.expose_function("browseSavedControls", respond)
    page.evaluate("""() => {
      const send = window.testSocket.send.bind(window.testSocket);
      window.testSocket.send = data => {
        send(data);
        const request = JSON.parse(data);
        if (request.type === 'compliance.controls') window.browseSavedControls(request).then(payload =>
          window.testSocket.emit({type: 'compliance.controls', payload}));
      };
    }""")
    emit(page, "compliance.snapshot", {"context_id": "context-1", "runs": [record]})
    return record


@pytest.mark.parametrize("width", [390, 1440])
def test_complete_browser_pages_searches_and_filters_original_evidence(
    dashboard, tmp_path, width
):
    page = dashboard
    page.set_viewport_size({"width": width, "height": 1440 if width > 760 else 1100})
    _show_complete_record(page, tmp_path)
    expect(page.locator("#compliance-page-summary")).to_contain_text("275 total")
    page.locator("#compliance-control-filter").select_option("all")
    page.locator("#compliance-page-size").select_option("25")
    page.locator("#compliance-control-sort").select_option("control_id")
    expect(page.locator("#compliance-page-summary")).to_have_text(
        "1–25 of 275 matching checks · 275 total"
    )
    page.locator("#compliance-page-next").click()
    expect(page.locator("#compliance-page-number")).to_have_text("Page 2 of 11")
    expect(page.locator("#compliance-controls")).to_contain_text("control-025")
    page.locator("#compliance-control-search").fill("SSH")
    expect(page.locator("#compliance-page-summary")).to_have_text(
        "1–6 of 6 matching checks · 275 total"
    )
    expect(page.locator("#compliance-controls")).to_contain_text("control-274")
    expect(page.locator("#compliance-page-previous")).to_be_disabled()
    expect(page.locator("#compliance-page-next")).to_be_disabled()
    assert not page.evaluate(
        "window.testSocket.sent.some(message => message.type === 'compliance.run')"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#compliance-control-search").evaluate("node => node.blur()")
    page.screenshot(
        path=str(tmp_path / f"compliance-full-browser-{width}.png"), full_page=True
    )
    page.locator("#compliance-control-impact").select_option("high")
    expect(page.locator("#compliance-controls tr")).to_have_count(3)
    page.locator("#compliance-control-filter").select_option("failed")
    expect(page.locator("#compliance-controls tr")).to_have_count(1)
    page.locator("#compliance-controls-clear").click()
    expect(page.locator("#compliance-control-search")).to_have_value("")
    expect(page.locator("#compliance-control-impact")).to_have_value("all")
    expect(page.locator("#compliance-page-summary")).to_have_text(
        "1–25 of 275 matching checks · 275 total"
    )


def test_complete_browser_reports_changed_evidence_and_retries(dashboard, tmp_path):
    page = dashboard
    record = _show_complete_record(page, tmp_path)
    expect(page.locator("#compliance-page-summary")).to_contain_text("275 total")
    path = Path(record["before"]["report_artifact"])
    original = path.read_bytes()
    path.write_text("changed")
    page.locator("#compliance-control-search").fill("SSH")
    expect(page.locator("#compliance-filter-notice")).to_contain_text("SHA-256")
    expect(page.locator("#compliance-controls tr")).to_have_count(0)
    expect(page.locator("#compliance-page-next")).to_be_disabled()
    path.write_bytes(original)
    page.locator("#compliance-controls-retry").click()
    expect(page.locator("#compliance-page-summary")).to_contain_text(
        "2 matching checks"
    )


def test_complete_browser_discards_old_search_and_run_responses(dashboard):
    page = dashboard
    record = _show_record(page)
    record["before"] = {
        "report_artifact": "/workspace/source.json",
        "report_sha256": "a" * 64,
    }
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    first = last_request(page, "compliance.controls")
    page.locator("#compliance-control-search").fill("new search")
    emit(
        page,
        "compliance.controls",
        {
            **first,
            "controls": [record["controls"][0]],
            "total": 275,
            "matched": 275,
            "page": 1,
            "pages": 6,
            "page_size": 50,
        },
    )
    page.wait_for_function(
        "window.testSocket.sent.filter(message => message.type === 'compliance.controls').length === 2"
    )
    expect(page.locator("#compliance-controls tr")).to_have_count(0)
    second = last_request(page, "compliance.controls")
    record["id"] = "b" * 32
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    emit(page, "compliance.controls", {**second, "detail": "stale failure"})
    expect(page.locator("#compliance-filter-notice")).not_to_contain_text(
        "stale failure"
    )


def test_complete_browser_disconnect_discards_pending_page(dashboard):
    page = dashboard
    record = _show_record(page)
    record["before"] = {
        "report_artifact": "/workspace/source.json",
        "report_sha256": "a" * 64,
    }
    emit(page, "compliance.updated", {"context_id": "context-1", "run": record})
    request = last_request(page, "compliance.controls")
    page.evaluate("window.testSocket.close()")
    emit(page, "compliance.controls", {**request, "detail": "stale failure"})
    expect(page.locator("#compliance-filter-notice")).to_contain_text("Connection lost")
    expect(page.locator("#compliance-page-next")).to_be_disabled()
    expect(page.locator("#compliance-run-review")).to_have_attribute(
        "aria-busy", "false"
    )
