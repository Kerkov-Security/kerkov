"""Exercise the rendered Agents panel without a server or provider connection."""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

django.setup()

from tests.web.browser_support import emit  # noqa: E402


def agent_snapshot(**changes: Any) -> dict[str, Any]:
    return {
        "id": "agent-config",
        "name": "config-review",
        "task": "Review authentication configuration and report findings.",
        "profile": "research",
        "state": "running",
        "current_tool": "read_file",
        "iteration": 2,
        "max_iterations": 12,
        "report": None,
        **changes,
    }


def select_text(page: Page, selector: str) -> None:
    page.locator(selector).evaluate("""element => {
          const range = document.createRange();
          range.selectNodeContents(element);
          window.getSelection().removeAllRanges();
          window.getSelection().addRange(range);
        }""")


def test_live_updates_preserve_expansion_focus_and_selected_text(
    dashboard: Page,
) -> None:
    page = dashboard
    expect(page.locator("#agents-empty")).to_be_visible()
    first = agent_snapshot()
    second = agent_snapshot(
        id="agent-routes", name="route-audit", state="queued", current_tool=None
    )
    emit(page, "agent.updated", {"agent": first})
    emit(page, "agent.updated", {"agent": second})
    rows = page.locator(".agent-row")
    expect(rows).to_have_count(2)
    expect(page.locator("#agent-count")).to_have_text("2")
    expect(page.locator("#agents-status")).to_have_text("2 active")
    expect(rows.nth(1).locator(".agent-state")).to_have_text("Queued")

    first_toggle = rows.nth(0).locator(".agent-toggle")
    first_toggle.focus()
    first_toggle.press("Enter")
    expect(first_toggle).to_have_attribute("aria-expanded", "true")
    expect(rows.nth(0).locator(".agent-task")).to_have_text(first["task"])
    select_text(page, ".agent-row:first-child .agent-task")

    emit(
        page,
        "agent.updated",
        {"agent": {**first, "iteration": 3, "current_tool": "search_files"}},
    )
    expect(first_toggle).to_be_focused()
    expect(first_toggle).to_have_attribute("aria-expanded", "true")
    expect(rows.nth(0).locator(".agent-step")).to_have_text("3 of 12")
    expect(rows.nth(0).locator(".agent-tool")).to_have_text("search_files")
    assert page.evaluate("window.getSelection().toString()") == first["task"]

    emit(page, "agent.updated", {"agent": {**second, "state": "blocked"}})
    expect(rows.nth(1).locator(".agent-state")).to_have_text("Awaiting input")
    second_toggle = rows.nth(1).locator(".agent-toggle")
    second_toggle.focus()
    second_toggle.press("Space")
    expect(second_toggle).to_have_attribute("aria-expanded", "true")
    expect(first_toggle).to_have_attribute("aria-expanded", "false")
    expect(rows.nth(0).locator(".agent-detail")).to_be_hidden()


def test_reports_are_keyboard_accessible_inert_and_cleared_on_reset(
    dashboard: Page,
) -> None:
    page = dashboard
    hostile = '<img src=x onerror="window.agentInjected=true">'
    findings = (
        f"Untrusted tool output: {hostile}\n<script>window.agentInjected=true</script>"
    )
    report = {
        "status": "completed",
        "findings": findings,
        "next_steps": f"Check the literal output {hostile}",
        "tokens_used": 360,
    }
    snapshot = agent_snapshot(
        name=f"config-review {hostile}",
        task=f"Review {hostile}",
        state="done",
        current_tool=None,
        report=report,
    )
    emit(page, "agent.updated", {"agent": snapshot})
    toggle = page.locator(".agent-toggle")
    toggle.focus()
    toggle.press("Enter")
    expect(page.locator(".agent-task")).to_have_text(snapshot["task"])
    open_report = page.get_by_role(
        "button", name=f"Open report from {snapshot['name']}", exact=True
    )
    open_report.focus()
    open_report.press("Enter")
    report_panel = page.locator("#agent-report")
    close_report = page.get_by_role("button", name="Close agent report")
    expect(report_panel).to_be_visible()
    expect(close_report).to_be_focused()
    expect(page.locator("#agent-report-name")).to_have_text(snapshot["name"])
    expect(page.locator("#agent-report-status")).to_have_text("Completed")
    assert page.locator("#agent-report-findings").text_content() == findings
    expect(page.locator("#agent-report-next-text")).to_have_text(report["next_steps"])
    expect(page.locator("#agent-list img, #agent-list script")).to_have_count(0)
    expect(report_panel.locator("img, script")).to_have_count(0)
    assert page.evaluate("window.agentInjected === undefined")

    select_text(page, "#agent-report-findings")
    emit(
        page,
        "agent.updated",
        {"agent": {**snapshot, "report": {**report, "tokens_used": 361}}},
    )
    expect(close_report).to_be_focused()
    assert page.evaluate("window.getSelection().toString()") == findings
    close_report.press("Escape")
    expect(report_panel).to_be_hidden()
    expect(open_report).to_be_focused()
    open_report.press("Space")
    close_report.press("Enter")
    expect(report_panel).to_be_hidden()
    expect(open_report).to_be_focused()

    open_report.press("Enter")
    emit(page, "agents.reset", {})
    expect(report_panel).to_be_hidden()
    expect(page.locator(".agent-row")).to_have_count(0)
    expect(page.locator("#agent-count")).to_have_text("0")
    expect(page.locator("#agents-empty")).to_be_visible()
    expect(page.locator("#agent-report-findings")).to_have_text("")
    expect(page.locator("#message-stream")).to_be_focused()


def test_disconnect_keeps_reports_readable_and_mobile_layout_within_viewport(
    dashboard: Page,
) -> None:
    page = dashboard
    page.set_viewport_size({"width": 390, "height": 844})
    active = agent_snapshot(name="long-agent-" + "x" * 150, task="scope/" * 100)
    completed = agent_snapshot(
        id="agent-done",
        name="route-audit",
        state="done",
        current_tool=None,
        report={
            "status": "partial",
            "findings": "Long unbroken finding: " + "x" * 500,
            "next_steps": "",
            "tokens_used": 250,
        },
    )
    emit(page, "agent.updated", {"agent": active})
    emit(page, "agent.updated", {"agent": completed})
    page.locator(".agent-toggle").first.click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.evaluate("window.testSocket.close()")
    expect(page.locator("#agents-status")).to_have_text("Offline")
    expect(page.locator(".agent-state").first).to_have_text("Disconnected")
    expect(page.locator(".agent-current").first).to_have_text("Updates unavailable")
    expect(page.locator(".agent-state").nth(1)).to_have_text("Done")
    page.locator(".agent-toggle").nth(1).click()
    page.get_by_role("button", name="Open report from route-audit").click()
    expect(page.locator("#agent-report")).to_be_visible()
    expect(page.locator("#agent-report-status")).to_have_text("Partial report")
    expect(page.locator("#agent-report-next")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
